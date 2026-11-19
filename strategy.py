"""Дневной цикл MAJORITY и восстановление заявок по данным биржи."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pandas as pd

from settings import POSITION_FRACTION, STOP_FRACTION
from trader import D, FINAL, ms, number


class TradingBot:
    def __init__(self, trader, journal, models, history, notify, log, clock=None):
        self.trader = trader
        self.journal = journal
        self.models = models
        self.history = history
        self.notify = notify
        self.log = log
        self.enabled = False
        self.reconciled = False
        self.next_close_retry = None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled_since = None

    def _known_stop(self, now, side, since):
        start = max(since, ms(now - timedelta(days=7)) + 1000)
        if self.trader.stop_execution(start) is None:
            return None
        return dict(qty="0", anchor=None, episode=None, blocked=side, target=None)

    def resolve_order(self, now):
        op = self.journal.get("pending")
        if op is None:
            return
        row = self.trader.order(op["id"])
        if row is None or row["orderStatus"] not in FINAL:
            raise RuntimeError(f"Результат заявки {op['id']} ещё не подтверждён")
        filled = number(row["cumExecQty"])
        before = number(op["before"])
        qty = before + op["side"] * filled
        position = self.trader.position()
        states = {"qty": str(qty)}
        if position.qty != qty:
            side = (qty > 0) - (qty < 0)
            stopped = self._known_stop(now, side, op["sent"]) if not position.qty and side else None
            if stopped is None:
                raise RuntimeError("Позиция после заявки расходится с подтверждённым исполнением")
            states.update(stopped)
            self.notify("Биржа исполнила SL во время обработки заявки.")
        elif not qty:
            states.update(anchor=None, episode=None)
        elif not before:
            anchor = number(row["avgPrice"])
            if anchor <= 0:
                raise RuntimeError("Биржа ещё не вернула цену первоначального входа")
            states.update(anchor=str(anchor), episode=op["sent"])
        if not filled:
            if not self.journal.get("close_requested"):
                states["target"] = None
            self.notify(f"Заявка {op['id']} завершена без сделки ({row['orderStatus']}).")
        self.journal.finish_order(op, row, **states)
        self.log.info(f"Исполнение {op['id']} | {row['orderStatus']} | {filled} ETH | позиция {position.qty}")

    def reconcile(self, now):
        self.reconciled = False
        self.resolve_order(now)
        position = self.trader.position()
        if self.trader.ordinary_orders():
            raise RuntimeError("Обнаружены активные заявки вне обрабатываемого намерения")
        recorded = number(self.journal.get("qty"))
        if position.qty != recorded:
            side = (recorded > 0) - (recorded < 0)
            stopped = self._known_stop(now, side, self.journal.get("episode", 0)) if not position.qty and side else None
            if stopped is None:
                raise RuntimeError(f"Позиция на Bybit {position.qty} ETH, в журнале {recorded} ETH")
            self.journal.update(**stopped)
            self.notify("Подтверждено закрытие по защитному SL. Прежнее направление заблокировано.")
        if position.qty and not self.journal.get("close_requested"):
            position = self.protect(position)
        self.reconciled = True
        return position

    def protect(self, position):
        anchor = number(self.journal.get("anchor"))
        if anchor <= 0:
            raise RuntimeError("Отсутствует первоначальная цена входа")
        level = self.trader.stop_price(anchor, position.side, D(STOP_FRACTION))
        if position.stop != level:
            self.trader.set_stop(level)
            updated = self.trader.position()
            if updated.qty != position.qty or updated.stop != level:
                raise RuntimeError(f"Биржа не подтвердила SL {level} для всей позиции")
            self.log.info(f"Защитный SL подтверждён | {level} USDT | позиция {updated.qty} ETH")
            return updated
        return position

    def submit(self, position, target, now):
        change = target - position.qty
        if not change:
            self.journal.update(target=None)
            return
        side = 1 if change > 0 else -1
        reducing = bool(position.qty and side != position.side)
        closing = reducing and (not target or position.side * target < 0)
        requested = -position.qty if closing else change
        qty = self.trader.quantity(requested, full_close=closing)
        if not qty or (not reducing and not self.trader.meets_notional(qty, side)):
            self.journal.update(target=None)
            self.notify("Ребалансировка пропущена. Разница меньше минимального объёма Bybit.")
            return
        stop = self.trader.stop_price(self.trader.price(side), side, D(STOP_FRACTION)) if not position.qty else None
        op = dict(id="m60-" + uuid4().hex, before=str(position.qty), side=side, qty=format(qty, "f"),
                  reduce=reducing, entry_stop=format(stop, "f") if stop else None,
                  sent=ms(now))
        # Повторные открытия и ребалансировки за день не наращивают объём после частичного IOC.
        # Если закрываем для переворота, сначала подтверждаем нулевую позицию.
        self.journal.begin_order(op, **({} if closing else {"target": None}))
        result = self.trader.place(op)
        self.log.info(f"Bybit принял {op['id']} | запись заявки {result['result'].get('orderId')}")

    def daily_decision(self, now, position):
        day = pd.Timestamp(now).floor("D")
        key = day.date().isoformat()
        if self.journal.get("last_day") == key:
            return
        if now.hour >= 1:
            return
        result = self.models.for_day(day).decide(self.history.window(day))
        side = result["side"]
        blocked = self.journal.get("blocked", 0)
        if not side or side != blocked:
            blocked = 0
        result["allowed_side"] = side if side != blocked else 0
        target = self.trader.target(result["allowed_side"], D(POSITION_FRACTION))
        finished_at = self.clock()
        if finished_at >= day + pd.Timedelta(hours=1):
            raise RuntimeError("Получен прогноз после завершения торгового окна")
        result.update(target_eth=str(target), previous_eth=str(position.qty), at=finished_at.isoformat())
        self.journal.decision(key, result, blocked=blocked, target=str(target))
        self.log.info(f"Решение {key} | {result}")
        self.notify(f"{key} UTC | MOMENTUM60 {result['direction']:+d} | "
                    f"голоса {sum(result['votes'])}/5 | цель {target} ETH.")

    def tick(self, now):
        day = now.date().isoformat()
        cutoff = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if (self.enabled and self.journal.get("last_day") != day
                and self.enabled_since < cutoff <= now):
            self.journal.decision(day, {"reason": "MISSED", "side": 0}, close_requested=True, target=None)
            self.notify(f"К 01:00 UTC за {day} нет исполнимого дневного решения. Запрошено закрытие.")
        position = self.reconcile(now)
        if self.journal.get("close_requested"):
            if not position.qty:
                self.journal.update(close_requested=False, target=None)
                self.notify("Нулевая позиция подтверждена.")
            elif self.next_close_retry is None or now >= self.next_close_retry:
                self.next_close_retry = now + timedelta(seconds=30)
                self.submit(position, D(0), now)
            return
        if not self.enabled:
            return
        self.daily_decision(now, position)
        now = self.clock()
        value = self.journal.get("target")
        if value is None or self.journal.get("last_day") != day:
            return
        target = D(value)
        if now.hour >= 1 or now.date().isoformat() != day:
            self.journal.update(target=None)
            if position.qty and (not target or target * position.qty < 0):
                self.journal.update(close_requested=True)
            return
        self.submit(position, target, now)

    def start(self, now):
        self.reconcile(now)
        if self.journal.get("close_requested"):
            raise RuntimeError("Сначала необходимо подтвердить запрошенное закрытие")
        if self.enabled:
            return
        self.enabled_since = now
        self.enabled = True
        self.notify("Торговля включена. Ожидается своевременное дневное решение.")

    def pause(self, close=False):
        self.enabled = False
        if close:
            self.journal.update(target=None, close_requested=True)
        else:
            self.journal.update(target=None)

    def sync_finance(self, now):
        initial = self.journal.get("finance_since")
        if initial is None:
            initial = now.isoformat()
            self.journal.update(finance_since=initial, finance_through=initial)
        through = datetime.fromisoformat(self.journal.get("finance_through"))
        since = max(through - timedelta(days=1), datetime.fromisoformat(initial))
        entries = list(self.trader.transactions(since, now))
        self.journal.record_finance(entries, now.isoformat())

    def status(self, now):
        pos = self.trader.position()
        decision = self.journal.get("last_decision") or {}
        pending = self.journal.get("pending")
        valid_until = decision.get("valid_until", "—")
        try:
            valid_until = self.models.for_day(pd.Timestamp(now).floor("D")).end.isoformat()
        except ValueError:
            valid_until = f"{valid_until} (актуальный комплект недоступен)"
        return (
            f"Торговля {'включена' if self.enabled else 'на паузе'}\n"
            f"Позиция {pos.qty} ETH, средняя {pos.average} USDT, SL {pos.stop} USDT\n"
            f"Первоначальный вход {self.journal.get('anchor') or '—'}\n"
            f"Последнее решение {self.journal.get('last_day', '—')} UTC\n"
            f"Направление {decision.get('direction', '—')}, голоса {decision.get('votes', '—')}\n"
            f"Модели до {valid_until}\n"
            f"Блокировка после SL {self.journal.get('blocked', 0)}\n"
            f"Ожидание заявки {pending['id'] if pending else '—'}\n"
            f"Сверка {'выполнена' if self.reconciled else 'ожидается'}"
        )
