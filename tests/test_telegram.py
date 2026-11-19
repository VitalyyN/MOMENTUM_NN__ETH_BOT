import os
import sys
import unittest
from unittest.mock import Mock, patch

import telebot
from telebot import apihelper

from main import main, telegram_client
from trader import make_clients


class TelegramProxyTests(unittest.TestCase):
    def test_proxy_applies_to_telegram_polling_and_delivery_only(self):
        proxy_url = "http://proxy.example:3128"
        environment = dict(TELEGRAM_TOKEN="123:example-token", TELEGRAM_PROXY=proxy_url,
                           BYBIT_DEMO_API_KEY="example", BYBIT_DEMO_API_SECRET="example-secret")
        session = Mock()
        session.request.return_value.json.side_effect = [
            {"ok": True, "result": dict(id=123, is_bot=True, username="example_bot", first_name="Example")},
            {"ok": True, "result": []},
            {"ok": True, "result": {}},
        ]
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(apihelper, "proxy", None),
              patch.object(apihelper, "_get_req_session", return_value=session)):
            bot = telegram_client(Mock())
            self.assertEqual(bot.get_me().username, "example_bot")
            bot.get_updates()
            apihelper.send_message(bot.token, 10, "test")
            self.assertEqual(session.request.call_count, 3)
            for request in session.request.call_args_list:
                self.assertEqual(request.kwargs["proxies"], {"https": proxy_url})
            self.assertEqual(os.environ.copy(), environment)
            _, bybit = make_clients(True, os.environ)
            self.assertEqual(bybit.read.client.proxies, {})
            os.environ["TELEGRAM_PROXY"] = ""
            telegram_client(Mock())
            self.assertIsNone(apihelper.proxy)

    def test_socks5_proxy_requires_optional_dependency(self):
        environment = dict(TELEGRAM_TOKEN="123:example-token",
                           TELEGRAM_PROXY="socks5h://127.0.0.1:1080")
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(apihelper, "proxy", None),
              patch("main.find_spec", return_value=None) as find_socks):
            with self.assertRaisesRegex(ValueError, "PySocks==1.7.1"):
                telegram_client(Mock())
            self.assertIsNone(apihelper.proxy)
            find_socks.return_value = Mock()
            telegram_client(Mock())
            self.assertEqual(apihelper.proxy, {"https": environment["TELEGRAM_PROXY"]})

    def test_check_telegram_masks_credentials_in_error(self):
        token = "123:example-token"
        proxy_url = "http://tester:example%2Bpassword@proxy.example:3128"
        environment = dict(TELEGRAM_TOKEN=token, TELEGRAM_PROXY=proxy_url)
        log = Mock()
        session = Mock()
        session.request.side_effect = RuntimeError(f"{token} {proxy_url} example+password")
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(apihelper, "proxy", None),
              patch.object(apihelper, "_get_req_session", return_value=session),
              patch.object(telebot.logger, "filters", []),
              patch.object(sys, "argv", ["main.py", "--check-telegram"]),
              patch("main.setup_logging", return_value=log),
              patch("main.load_dotenv"),
              patch("main.make_clients") as bybit):
            with self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 1)
            log.error.assert_called_once_with("[скрыто] [скрыто] [скрыто]")
            bybit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
