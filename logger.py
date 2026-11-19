"""Дневная ротация файлов и сообщения с меткой режима."""

import sys

from loguru import logger

from settings import ROOT


def setup_logging(mode):
    directory = ROOT / "logs" / mode.lower()
    directory.mkdir(parents=True, exist_ok=True)
    layout = "{time:YYYY-MM-DD HH:mm:ss Z} | {extra[mode]} | {level} | {message}"
    logger.remove()
    logger.configure(extra={"mode": mode})
    logger.add(directory / "bot_{time:YYYY-MM-DD}.log", rotation="00:00", retention="30 days",
               compression="zip", level="INFO", format=layout, encoding="utf-8", diagnose=False)
    if sys.stderr is not None:
        logger.add(sys.stderr, level="INFO", format=layout, diagnose=False)
    return logger
