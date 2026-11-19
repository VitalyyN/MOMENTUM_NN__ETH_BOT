"""Раздельные сессии для публичных данных и выбранного торгового счёта."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
from datetime import timedelta

from pybit.unified_trading import HTTP

from settings import SYMBOL, mode

D = Decimal
FINAL = {"Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated"}


def number(value):
    result = D(value or "0")
    if not result.is_finite():
        raise ValueError("Некорректное числовое значение в ответе Bybit")
    return result


def ms(timestamp):
    return int(timestamp.timestamp() * 1000)


def step(value, increment, rounding=ROUND_FLOOR):
    return (value / increment).to_integral_value(rounding=rounding) * increment


@dataclass(frozen=True)
class Position:
    qty: Decimal
    average: Decimal = D(0)
    stop: Decimal = D(0)
    unrealized: Decimal = D(0)

    @property
    def side(self):
        return (self.qty > 0) - (self.qty < 0)


def make_clients(demo, environ, http=HTTP):
    if type(demo) is not bool:
        raise ValueError("Флаг DEMO должен быть логическим")
    prefix = f"BYBIT_{mode(demo)}_API_"
    key, secret = (environ.get(prefix + suffix, "").strip() for suffix in ("KEY", "SECRET"))
    if not key or not secret:
        raise ValueError(f"Заполните {prefix}KEY и {prefix}SECRET")
    public = http(testnet=False, demo=False, timeout=8)
    common = dict(testnet=False, demo=demo, api_key=key, api_secret=secret, timeout=8,
                  log_requests=False)
    private = http(**common)
    # Одна HTTP-попытка для записей. Повтор решает журнал после сверки.
    writes = http(**common, force_retry=False, max_retries=1)
    return public, Trader(private, writes, public, hashlib.sha256(key.encode()).hexdigest())


class Trader:
    def __init__(self, read, write, public, account):
        self.read = read
        self.write = write
        self.public = public
        self.account = account
        self.instrument = dict(category="linear", symbol=SYMBOL)
        self.spec = None

    def specs(self):
        if self.spec is None:
            row, = self.public.get_instruments_info(**self.instrument)["result"]["list"]
            if (row["contractType"] != "LinearPerpetual" or row["settleCoin"] != "USDT"
                    or row["status"] != "Trading"):
                raise ValueError("Ожидался торгуемый бессрочный ETHUSDT с расчётом в USDT")
            self.spec = row
        return self.spec

    def position(self):
        rows = self.read.get_positions(**self.instrument)["result"]["list"]
        if len(rows) != 1 or int(rows[0]["positionIdx"]) != 0:
            raise ValueError("Для ETHUSDT требуется режим one-way")
        row = rows[0]
        size = number(row["size"])
        if number(row["leverage"]) != 1:
            raise ValueError("Для ETHUSDT требуется плечо 1×")
        if size and (row["side"] not in ("Buy", "Sell") or number(row["avgPrice"]) <= 0):
            raise ValueError("Не удалось определить сторону и среднюю позиции")
        qty = size if row["side"] == "Buy" else -size
        return Position(qty, number(row["avgPrice"]), number(row["stopLoss"]), number(row["unrealisedPnl"]))

    def ordinary_orders(self):
        orders = []
        for order_filter in ("Order", "StopOrder"):
            result = self.read.get_open_orders(**self.instrument, orderFilter=order_filter,
                                               openOnly=0, limit=50)["result"]
            orders.extend(row for row in result["list"] if row.get("stopOrderType") != "StopLoss")
        return orders

    def equity(self):
        account, = self.read.get_wallet_balance(accountType="UNIFIED", coin="USDT")["result"]["list"]
        coin, = account["coin"]
        if coin["coin"] != "USDT":
            raise ValueError("Не найден USDT в выделенном счёте")
        return number(coin["equity"])

    def price(self, side):
        row, = self.public.get_tickers(**self.instrument)["result"]["list"]
        price = number(row["ask1Price" if side > 0 else "bid1Price"])
        if price <= 0:
            raise ValueError("Нет котировки фьючерса для расчёта заявки")
        return price

    def target(self, side, fraction):
        if not side:
            return D(0)
        equity = self.equity()
        if equity <= 0:
            raise ValueError("Капитал счёта должен быть положительным")
        return side * step(equity * fraction / self.price(side), self.quantity_step)

    @property
    def quantity_step(self):
        return number(self.specs()["lotSizeFilter"]["qtyStep"])

    def quantity(self, requested, full_close):
        lot = self.specs()["lotSizeFilter"]
        qty = min(abs(requested), number(lot["maxMktOrderQty"]))
        if full_close and qty == abs(requested):
            return qty
        qty = step(qty, self.quantity_step)
        if qty < number(lot["minOrderQty"]):
            return D(0)
        return qty

    def meets_notional(self, qty, side):
        lot = self.specs()["lotSizeFilter"]
        return qty * self.price(side) >= number(lot["minNotionalValue"])

    def stop_price(self, anchor, side, fraction):
        tick = number(self.specs()["priceFilter"]["tickSize"])
        return step(anchor * (1 - side * fraction), tick, ROUND_CEILING if side > 0 else ROUND_FLOOR)

    def place(self, op):
        payload = dict(
            **self.instrument, positionIdx=0, orderType="Market", orderLinkId=op["id"],
            side="Buy" if op["side"] > 0 else "Sell", qty=op["qty"],
            reduceOnly=op["reduce"],
        )
        if op["entry_stop"]:
            payload.update(stopLoss=op["entry_stop"], tpslMode="Full",
                           slOrderType="Market", slTriggerBy="LastPrice")
        return self.write.place_order(**payload)

    def order(self, link):
        for endpoint in (self.read.get_open_orders, self.read.get_order_history):
            rows = endpoint(**self.instrument, orderLinkId=link)["result"]["list"]
            for row in rows:
                if row["orderLinkId"] == link:
                    return row
        return None

    def set_stop(self, level):
        self.write.set_trading_stop(
            **self.instrument, positionIdx=0, tpslMode="Full",
            stopLoss=format(level, "f"), slOrderType="Market", slTriggerBy="LastPrice",
        )

    def stop_execution(self, since):
        rows = self.read.get_order_history(**self.instrument, orderFilter="StopOrder",
                                           startTime=since, limit=50)["result"]["list"]
        return next((row for row in rows
                     if row.get("stopOrderType") == "StopLoss"
                     and row["orderStatus"] == "Filled" and int(row["updatedTime"]) >= since), None)

    def transactions(self, start, end):
        while start < end:
            until = min(start + timedelta(days=6), end)
            cursor = ""
            while True:
                filters = dict(accountType="UNIFIED", category="linear", currency="USDT",
                               startTime=ms(start), endTime=ms(until), limit=50)
                if cursor:
                    filters["cursor"] = cursor
                result = self.read.get_transaction_log(**filters)["result"]
                yield from (row for row in result["list"]
                            if row["symbol"] == SYMBOL and row["type"] in ("TRADE", "SETTLEMENT"))
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            start = until
