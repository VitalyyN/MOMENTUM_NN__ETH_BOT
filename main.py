"""Запуск робота, управление через Telegram и восстановление."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from importlib.util import find_spec
from pathlib import Path
import os
from queue import Empty, Queue
import threading
import time
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv
import pandas as pd
from pybit.unified_trading import HTTP
import telebot
from telebot import apihelper, types

import settings as cfg
from journal import Journal
from logger import setup_logging
from market import SpotHistory
from strategy import TradingBot
from trader import D, make_clients

START = "Start_bot"
SAVE = "Stop_&_Save"
CLOSE = "Stop_&_Close"
COMMANDS = {
    START: START, "/start_bot": START, SAVE: SAVE, CLOSE: CLOSE, "/stop": SAVE,
    "Status": "Status", "/status": "Status", "/start": "Status",
    "Balance": "Balance", "/balance": "Balance", "PnL": "PnL", "/pnl": "PnL",
}


def now():
    return datetime.now(timezone.utc)


@contextmanager
def instance(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"\x00")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Робот уже запущен") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("Робот уже запущен") from error
        yield


def telegram_client(log):
    proxy_url = os.getenv("TELEGRAM_PROXY", "").strip()
    proxy = None
    if proxy_url:
        try:
            address = urlsplit(proxy_url)
            port = address.port
            if (address.scheme not in ("http", "https", "socks5", "socks5h")
                    or not address.hostname or not port or address.path not in ("", "/")
                    or address.query or address.fragment or any(char.isspace() for char in proxy_url)):
                raise ValueError
        except ValueError:
            raise ValueError("TELEGRAM_PROXY должен быть URL HTTP(S) или SOCKS5 с адресом и портом.") from None
        if address.scheme in ("socks5", "socks5h") and find_spec("socks") is None:
            raise ValueError("Для SOCKS5 установите PySocks в окружение робота: "
                             ".venv\\Scripts\\python.exe -m pip install PySocks==1.7.1")
        proxy = {"https": proxy_url}
        log.info("Telegram использует прокси из TELEGRAM_PROXY.")
    apihelper.proxy = proxy
    return telebot.TeleBot(os.environ["TELEGRAM_TOKEN"], threaded=False)


class Telegram:
    def __init__(self, mode, log, sanitize):
        self.mode = mode
        self.log = log
        self.sanitize = sanitize
        self.commands = Queue()
        self.outbox = Queue()
        self.boot = int(time.time())
        self.chat = int(os.environ["TELEGRAM_CHAT_ID"])
        self.user = int(os.environ["TELEGRAM_USER_ID"])
        self.bot = telegram_client(log)
        self.keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        self.keyboard.row(START, SAVE, CLOSE)
        self.keyboard.row("Status", "Balance", "PnL")
        self.bot.register_message_handler(self.on_message, content_types=["text"])

    def on_message(self, message):
        if (message.chat.type != "private" or message.chat.id != self.chat
                or not message.from_user or message.from_user.id != self.user
                or message.date <= self.boot):
            return
        command = COMMANDS.get(message.text.strip())
        if command:
            self.commands.put(command)

    def notify(self, text):
        self.outbox.put(f"[{self.mode}] {self.sanitize(text)}")

    def send(self):
        while True:
            message = self.outbox.get()
            while True:
                try:
                    self.bot.send_message(self.chat, message, reply_markup=self.keyboard, timeout=12)
                    break
                except Exception as error:
                    self.log.warning(f"Telegram, пауза доставки | {type(error).__name__}")
                    time.sleep(15)

    def start(self):
        polling = threading.Thread(target=self.bot.infinity_polling,
                                   kwargs=dict(skip_pending=False, timeout=20, long_polling_timeout=20,
                                               logger_level=50), daemon=True)
        polling.start()
        threading.Thread(target=self.send, daemon=True).start()


def handle(command, bot, notify):
    if command == START:
        bot.start(now())
    elif command == SAVE:
        bot.pause()
        notify("Торговля на паузе. Открытая позиция остаётся под биржевым SL.")
    elif command == CLOSE:
        bot.pause(close=True)
        notify("Торговля на паузе. Запрошено закрытие позиции.")
    elif command == "Status":
        notify(bot.status(now()))
    elif command == "Balance":
        notify(f"Капитал счёта {bot.trader.equity():.2f} USDT")
    elif command == "PnL":
        bot.sync_finance(now())
        cash, fee, received, paid = bot.journal.finance()
        open_pnl = bot.trader.position().unrealized
        notify(f"Журнал с {bot.journal.get('finance_since')}\n"
               f"Реализованный торговый PnL {cash:.2f} USDT\n"
               f"Комиссии {fee:.2f} USDT\nФандинг +{received:.2f} / −{paid:.2f} USDT\n"
               f"Открытый PnL {open_pnl:.2f} USDT\n"
               f"Чистый реализованный PnL {cash - fee + received - paid:.2f} USDT")


def run(mode, telegram, log, sanitize):
    from models import Models

    telegram.start()
    try:
        public, trader = make_clients(cfg.DEMO, os.environ)
        journal = Journal(cfg.ROOT / "state" / f"{mode.lower()}.sqlite3", trader.account)
    except (ValueError, OSError) as error:
        message = f"Запуск заблокирован. {sanitize(error)}"
        telegram.notify(message)
        log.error(message)
        while True:
            telegram.commands.get()
            telegram.notify(message)
    models = Models(cfg.MODEL_DIR)
    history = SpotHistory(public, cfg.ROOT / "cache" / "ethusdt_spot_hours.csv")
    bot = TradingBot(trader, journal, models, history, telegram.notify, log)
    announced = False
    last_error = None
    next_tick = now()
    next_accounting = now()
    if journal.get("finance_since") is None:
        initial = now().isoformat()
        journal.update(finance_since=initial, finance_through=initial)
    try:
        while True:
            try:
                command = telegram.commands.get(timeout=1)
            except Empty:
                command = None
            if command:
                try:
                    handle(command, bot, telegram.notify)
                except Exception as error:
                    telegram.notify(f"{command} | {sanitize(error)}")
            current = now()
            if current < next_tick:
                continue
            next_tick = current + timedelta(seconds=cfg.POLL_SECONDS)
            try:
                bot.tick(current)
                if not announced:
                    telegram.notify(f"Робот перезапущен. Нажмите {START} для возобновления.\n"
                                    f"{bot.status(current)}")
                    announced = True
                if last_error:
                    telegram.notify("Связь или сверка восстановлена.")
                    last_error = None
            except Exception as error:
                reason = sanitize(error)
                if reason != last_error:
                    log.warning(reason)
                    text = reason if announced else f"Робот перезапущен. Сверка ожидается. {reason}"
                    telegram.notify(text)
                    last_error = reason
            if current >= next_accounting:
                try:
                    bot.sync_finance(current)
                    next_accounting = current + timedelta(minutes=30)
                except Exception as error:
                    log.warning(f"Журнал операций | {sanitize(error)}")
                    next_accounting = current + timedelta(minutes=3)
    except KeyboardInterrupt:
        bot.pause()
        log.info("Процесс остановлен. Состояние сохранено.")
    finally:
        journal.close()


def preview(mode, day):
    from models import Models

    public = HTTP(testnet=False, demo=False, timeout=8)
    if day > pd.Timestamp.now(tz="UTC").floor("D") or day != day.floor("D"):
        raise ValueError("Нужна прошедшая или текущая UTC-дата решения")
    models = Models(cfg.MODEL_DIR)
    history = SpotHistory(public, cfg.ROOT / "cache" / "ethusdt_spot_hours.csv")
    result = models.for_day(day).decide(history.window(day))
    print(f"[{mode}] {day.date()} UTC | {result}")
    if os.getenv(f"BYBIT_{mode}_API_KEY") and os.getenv(f"BYBIT_{mode}_API_SECRET"):
        _, trader = make_clients(cfg.DEMO, os.environ)
        print(f"Целевое количество {trader.target(result['side'], D(cfg.POSITION_FRACTION))} ETH "
              f"| фактическая позиция {trader.position().qty} ETH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-models", action="store_true")
    action.add_argument("--check-telegram", action="store_true")
    action.add_argument("--preview", nargs="?", const="", metavar="YYYY-MM-DD")
    args = parser.parse_args()
    load_dotenv(cfg.ROOT / ".env")
    mode = cfg.mode(cfg.DEMO)
    log = setup_logging(mode)
    secrets = {value for name, value in os.environ.items()
               if value and (name.startswith("BYBIT_") or name == "TELEGRAM_TOKEN")}
    proxy_url = os.getenv("TELEGRAM_PROXY", "").strip()
    if proxy_url:
        secrets.add(proxy_url)
        try:
            password = urlsplit(proxy_url).password
        except ValueError:
            password = None
        if password:
            secrets.update((password, unquote(password)))
    secrets = sorted(secrets, key=len, reverse=True)

    def sanitize(value):
        value = str(value)
        for secret in secrets:
            value = value.replace(secret, "[скрыто]")
        return value

    def redact_telegram_log(record):
        record.msg = sanitize(record.getMessage())
        record.args = ()
        record.exc_info = record.exc_text = None
        return True

    telebot.logger.addFilter(redact_telegram_log)

    try:
        if args.check_models:
            from models import ModelBundle

            raw = (cfg.MODEL_DIR / "manifest.json").read_bytes()
            bundle = ModelBundle(cfg.MODEL_DIR, raw)
            print(f"Проверены пять ONNX. Период {bundle.start} — {bundle.end} UTC.")
        elif args.check_telegram:
            bot = telegram_client(log).get_me()
            print(f"Telegram API доступен. Бот @{bot.username}.")
        elif args.preview is not None:
            day = pd.Timestamp(args.preview, tz="UTC") if args.preview else pd.Timestamp.now(tz="UTC").floor("D")
            preview(mode, day)
        else:
            with instance(cfg.ROOT / "state" / "instance.lock"):
                log.info("Запуск на паузе. Сверяем данные с биржей.")
                run(mode, Telegram(mode, log, sanitize), log, sanitize)
    except Exception as error:
        log.error(sanitize(error))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
