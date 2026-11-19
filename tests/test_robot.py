from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from uuid import uuid4
from unittest.mock import Mock

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

from journal import Journal
from main import Telegram, instance
from market import OHLC, feature_window
from models import Models
from settings import MODEL_DIR, ROOT
from strategy import TradingBot
from trader import D, Position, make_clients


class FakeExchange:
    def __init__(self):
        self.pos = Position(D(0))
        self.orders = {}
        self.sent = []
        self.partial = None
        self.lost = False
        self.stopped = False

    def position(self):
        return self.pos

    def ordinary_orders(self):
        return []

    def target(self, side, fraction):
        return side * fraction * D(10000) / self.price(side)

    def price(self, side):
        return D(2000)

    def quantity(self, requested, full_close):
        return abs(requested)

    def meets_notional(self, qty, side):
        return True

    def stop_price(self, anchor, side, fraction):
        return anchor * (1 - side * fraction)

    def set_stop(self, level):
        self.pos = Position(self.pos.qty, self.pos.average, level)

    def order(self, link):
        return self.orders.get(link)

    def stop_execution(self, since):
        return {"orderStatus": "Filled"} if self.stopped else None

    def place(self, op):
        self.sent.append(op)
        qty = D(op["qty"])
        filled = min(qty, self.partial) if self.partial else qty
        new_qty = self.pos.qty + filled * op["side"]
        price = D(2050) if not self.pos.qty else D(2070)
        stop = D(op["entry_stop"]) if op["entry_stop"] else self.pos.stop
        self.pos = Position(new_qty, price, stop) if new_qty else Position(D(0))
        self.orders[op["id"]] = {
            "orderStatus": "Filled" if qty == filled else "PartiallyFilledCanceled",
            "cumExecQty": str(filled), "avgPrice": str(price),
        }
        if self.lost:
            raise TimeoutError("Потеря ответа после исполнения")
        return {"result": {"orderId": op["id"]}}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "state" / f"test-{uuid4().hex}"
        self.path = self.folder / "demo.sqlite3"
        self.exchange = FakeExchange()
        self.now = datetime(2026, 11, 12, 0, 1, tzinfo=timezone.utc)
        self.models = Mock()
        self.models.for_day.return_value.decide.side_effect = lambda x: dict(
            side=self.side, direction=self.side, votes=[True] * 5,
        )
        self.side = 1
        self.messages = []
        self.bot = self.restart()

    def tearDown(self):
        self.bot.journal.close()
        shutil.rmtree(self.folder)

    def restart(self):
        self.bot = TradingBot(self.exchange, Journal(self.path, "demo"), self.models, Mock(),
                              self.messages.append, Mock(), clock=lambda: self.now)
        return self.bot

    def opened(self):
        self.bot.start(self.now)
        self.bot.tick(self.now)
        self.bot.tick(self.now)

    def test_lost_ack_restart_recovers_partial_entry_and_initial_stop(self):
        self.exchange.lost = True
        self.exchange.partial = D("0.9")
        self.bot.start(self.now)
        with self.assertRaises(TimeoutError):
            self.bot.tick(self.now)
        self.bot.journal.close()
        self.restart()
        self.bot.tick(self.now)
        self.assertFalse(self.bot.enabled)
        self.assertEqual(self.exchange.pos.qty, D("0.9"))
        self.assertEqual(self.exchange.pos.stop, D(1640))
        self.assertEqual(self.bot.journal.get("anchor"), "2050")
        self.bot.start(self.now)
        self.bot.tick(self.now)
        self.assertEqual(len(self.exchange.sent), 1)

    def test_stop_save_then_restart_and_reduce_only_close(self):
        self.opened()
        self.bot.pause()
        self.bot.journal.close()
        self.restart()
        self.bot.tick(self.now)
        self.assertEqual(self.exchange.pos.stop, D(1640))
        self.assertEqual(len(self.exchange.sent), 1)
        self.bot.pause(close=True)
        self.bot.tick(self.now)
        self.assertTrue(self.exchange.sent[-1]["reduce"])
        self.bot.journal.close()
        self.restart()
        self.bot.tick(self.now)
        self.assertFalse(self.bot.journal.get("close_requested"))
        self.assertIn("Нулевая позиция подтверждена.", self.messages)

    def test_rebalance_preserves_stop_when_average_changes(self):
        self.exchange.partial = D(1)
        self.opened()
        self.exchange.partial = None
        self.now = self.now.replace(day=13)
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertEqual(self.exchange.pos.average, D(2070))
        self.assertEqual(self.exchange.pos.qty, D(2))
        self.assertEqual(self.exchange.pos.stop, D(1640))

    def test_reversal_waits_for_partial_close_to_finish(self):
        self.opened()
        self.side = -1
        self.exchange.partial = D(1)
        self.now = self.now.replace(day=13)
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertGreaterEqual(self.exchange.pos.qty, 0)
        self.assertTrue(all(order["reduce"] for order in self.exchange.sent[1:]))
        self.exchange.partial = None
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertEqual(self.exchange.pos.qty, D(-2))
        self.assertEqual(self.exchange.pos.stop, D(2460))

    def test_sl_during_downtime_blocks_same_side(self):
        self.opened()
        self.bot.journal.close()
        self.restart()
        self.exchange.pos = Position(D(0))
        self.exchange.stopped = True
        self.now = self.now.replace(day=13)
        self.bot.tick(self.now)
        self.assertEqual(self.bot.journal.get("blocked"), 1)
        self.bot.start(self.now)
        self.bot.tick(self.now)
        self.assertEqual(len(self.exchange.sent), 1)
        self.now = self.now.replace(day=14)
        self.side = 0
        self.bot.tick(self.now)
        self.assertEqual(self.bot.journal.get("blocked"), 0)
        self.now = self.now.replace(day=15)
        self.side = 1
        self.bot.tick(self.now)
        self.assertEqual(len(self.exchange.sent), 2)

    def test_expired_models_close_even_when_reconnected_after_cutoff(self):
        self.opened()
        self.now = self.now.replace(day=13)
        self.models.for_day.side_effect = ValueError("Истёк комплект")
        with self.assertRaises(ValueError):
            self.bot.tick(self.now)
        self.now = self.now.replace(hour=1)
        original = self.exchange.position
        self.exchange.position = Mock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            self.bot.tick(self.now)
        self.assertTrue(self.bot.journal.get("close_requested"))
        self.exchange.position = original
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertFalse(self.exchange.pos.qty)
        self.assertFalse(self.bot.journal.get("close_requested"))

    def test_stale_ack_blocks_retry(self):
        self.exchange.lost = True
        self.bot.start(self.now)
        with self.assertRaises(TimeoutError):
            self.bot.tick(self.now)
        self.exchange.orders.clear()
        self.bot.journal.close()
        self.restart()
        with self.assertRaisesRegex(RuntimeError, "не подтверждён"):
            self.bot.tick(self.now)
        self.assertEqual(len(self.exchange.sent), 1)

    def test_unknown_external_position_blocks_start(self):
        self.exchange.pos = Position(D(2), D(2000))
        with self.assertRaisesRegex(RuntimeError, "расходится|в журнале"):
            self.bot.start(self.now)
        self.assertFalse(self.bot.enabled)

    def test_funding_deduplication(self):
        item = dict(id="1", cashFlow="50", fee="2", funding="0")
        received = dict(id="2", cashFlow="0", fee="0", funding="0.4")
        paid = dict(id="3", cashFlow="0", fee="0", funding="-0.6")
        self.bot.journal.record_finance([item, received, paid], self.now.isoformat())
        self.bot.journal.record_finance([received, paid], self.now.isoformat())
        self.assertEqual(self.bot.journal.finance(), (D(50), D(2), D(".4"), D(".6")))

    def test_duplicate_instance(self):
        path = self.folder / "instance.lock"
        with instance(path):
            with self.assertRaises(RuntimeError):
                with instance(path):
                    pass

    def test_abrupt_exit_keeps_issued_intent_in_sqlite(self):
        script = (
            "from journal import Journal; import os, sys; from pathlib import Path; "
            "j = Journal(Path(sys.argv[1]), 'demo'); j.begin_order({'id': 'test-crash'}); os._exit(23)"
        )
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)], cwd=ROOT)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.bot.journal.get("pending"), {"id": "test-crash"})

    def test_stop_close_does_not_require_restoring_missing_protection_first(self):
        self.opened()
        self.exchange.pos = Position(self.exchange.pos.qty, D(2050))
        self.exchange.set_stop = Mock(side_effect=RuntimeError("Нет доступа к установке SL"))
        self.bot.pause(close=True)
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertEqual(self.exchange.pos.qty, 0)
        self.exchange.set_stop.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def test_pybit_modes_and_runtime_version(self):
        self.assertEqual(importlib.metadata.version("pybit"), "5.17.2")
        keys = {f"BYBIT_{mode}_API_{name}": f"{mode}-{name}"
                for mode in ("DEMO", "LIVE") for name in ("KEY", "SECRET")}
        spot, demo = make_clients(True, keys, http=HTTP)
        _, live = make_clients(False, keys, http=HTTP)
        self.assertEqual(spot.endpoint, "https://api.bybit.com")
        self.assertEqual(demo.write.endpoint, "https://api-demo.bybit.com")
        self.assertEqual(live.write.endpoint, "https://api.bybit.com")
        self.assertEqual(demo.read.api_key, "DEMO-KEY")
        self.assertEqual(live.read.api_key, "LIVE-KEY")
        self.assertNotEqual(demo.account, live.account)
        self.assertEqual(demo.write.max_retries, 1)
        with self.assertRaises(ValueError):
            make_clients(True, {})

    def test_same_spot_window_for_either_environment(self):
        bundle = Models(MODEL_DIR).for_day(pd.Timestamp("2026-11-19", tz="UTC"))
        with np.load(MODEL_DIR / "smoke_window.npz", allow_pickle=False) as smoke:
            window = smoke["F13"]
        self.assertEqual(bundle.decide(window)["side"], -1)
        self.assertEqual(sum(bundle.decide(window)["votes"]), 5)
        with self.assertRaises(ValueError):
            Models(MODEL_DIR).for_day(pd.Timestamp("2026-12-01", tz="UTC"))
        with self.assertRaises(ValueError):
            bundle.predict(window.astype(np.float32))

    def test_futures_price_and_account_equity_determine_qty(self):
        public_factory, demo = make_clients(True, {"BYBIT_DEMO_API_KEY": "TEST", "BYBIT_DEMO_API_SECRET": "TEST"})
        demo.spec = {"lotSizeFilter": {"qtyStep": ".01"}}
        demo.read = Mock()
        demo.public = Mock()
        demo.public.get_tickers.return_value = {"result": {"list": [{"ask1Price": "2010", "bid1Price": "2000"}]}}
        demo.read.get_wallet_balance.return_value = {"result": {"list": [{"coin": [{"coin": "USDT", "equity": "10000"}]}]}}
        self.assertEqual(demo.target(1, D(".40")), D("1.99"))
        self.assertEqual(demo.target(-1, D(".40")), D("-2"))
        self.assertFalse(public_factory.api_key)

    def test_partial_model_update_keeps_verified_bundle_only_until_expiry(self):
        folder = ROOT / "state" / f"bundle-{uuid4().hex}"
        shutil.copytree(MODEL_DIR, folder)
        try:
            loader = Models(folder)
            day = pd.Timestamp("2026-11-19", tz="UTC")
            first = loader.for_day(day)
            manifest = json.loads((folder / "manifest.json").read_bytes())
            manifest["SHA256"]["11.onnx"] = "wrong"
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIs(loader.for_day(day), first)
            with self.assertRaises(ValueError):
                loader.for_day(pd.Timestamp("2026-12-01", tz="UTC"))
        finally:
            shutil.rmtree(folder)

    def test_window_uses_130_complete_days_and_ignores_future(self):
        index = pd.date_range("2025-01-01", periods=24 * 132, tz="UTC", freq="h")
        prices = np.exp(np.arange(len(index)) / 12000 + .04 * np.sin(np.arange(len(index)) / 100)) * 2000
        hours = pd.DataFrame({name: prices for name in OHLC}, index=index)
        day = index[24 * 130]
        expected = feature_window(hours.loc[hours.index < day], day)
        hours.loc[hours.index >= day, list(OHLC)] = 10
        np.testing.assert_array_equal(feature_window(hours, day), expected)
        with self.assertRaises(ValueError):
            feature_window(hours.drop(index[1]), day)

    def test_telegram_requires_fresh_message_in_configured_private_chat(self):
        tg = Telegram.__new__(Telegram)
        tg.boot, tg.chat, tg.user, tg.commands = 100, 10, 11, Mock()
        msg = SimpleNamespace(date=100, text="Start_bot", chat=SimpleNamespace(type="private", id=10),
                              from_user=SimpleNamespace(id=11))
        tg.on_message(msg)
        msg.date = 101
        msg.from_user.id = 12
        tg.on_message(msg)
        tg.commands.put.assert_not_called()
        msg.from_user.id = 11
        tg.on_message(msg)
        tg.commands.put.assert_called_once_with("Start_bot")


if __name__ == "__main__":
    unittest.main()
