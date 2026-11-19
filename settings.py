"""Параметры запуска и стратегии."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO = True
SYMBOL = "ETHUSDT"
POSITION_FRACTION = "0.40"
STOP_FRACTION = "0.20"
POLL_SECONDS = 15
MODEL_DIR = ROOT / "models" / "current"


def mode(demo):
    return "DEMO" if demo else "LIVE"
