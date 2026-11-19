"""Проверка и атомарная замена комплекта из пяти моделей."""

import hashlib
import io
import json

import numpy as np
import onnxruntime as ort
import pandas as pd

from market import FEATURES, SEEDS, WINDOW

ORT_ERRORS = tuple(getattr(ort.capi.onnxruntime_pybind11_state, name) for name in (
    "Fail", "InvalidArgument", "InvalidGraph", "InvalidProtobuf", "NotImplemented", "RuntimeException",
))


def asset_digest(data):
    return hashlib.sha256(data).hexdigest()


def session(raw):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    return ort.InferenceSession(raw, sess_options=options, providers=["CPUExecutionProvider"])


class ModelBundle:
    def __init__(self, folder, manifest_bytes):
        meta = json.loads(manifest_bytes)
        if (meta["Instrument"] != "ETHUSDT" or tuple(meta["Features"]) != FEATURES
                or tuple(meta["Seeds"]) != SEEDS or meta["InputShape"] != ["batch", WINDOW, len(FEATURES)]
                or meta["InputDtype"] != "float64" or meta["VotesNeeded"] != 3
                or meta["RiskQuantile"] != .8 or meta["RiskTargetDays"] != 5
                or meta["CalibrationQuantile"] != .75):
            raise ValueError("Несовместимый протокол ONNX")
        self.start = pd.Timestamp(meta["ModelAsOf"])
        self.end = pd.Timestamp(meta["ModelValidUntilExclusive"])
        if (self.start.tzinfo is None or self.start.utcoffset().total_seconds() != 0
                or self.start != self.start.normalize() or self.start.day != 1
                or self.end != self.start + pd.offsets.MonthBegin()):
            raise ValueError("Неверный период действия моделей")
        self.thresholds = np.array([meta["Thresholds"][str(seed)] for seed in SEEDS], dtype=float)
        if not np.isfinite(self.thresholds).all() or (self.thresholds < 0).any():
            raise ValueError("Некорректные пороги моделей")
        self.identity = asset_digest(manifest_bytes)
        names = ["smoke_window.npz", *(f"{seed}.onnx" for seed in SEEDS)]
        assets = {}
        for name in names:
            raw = (folder / name).read_bytes()
            if asset_digest(raw) != meta["SHA256"][name]:
                raise ValueError(f"Контрольная сумма не совпала: {name}")
            assets[name] = raw
        self.sessions = [session(assets[f"{seed}.onnx"]) for seed in SEEDS]
        with np.load(io.BytesIO(assets["smoke_window.npz"]), allow_pickle=False) as smoke:
            actual = self.predict(smoke["F13"])[0]
            expected = smoke["expected"]
        if (expected.shape != (len(SEEDS),)
                or not np.allclose(actual, expected, rtol=1e-10, atol=1e-12)
                or not np.array_equal(actual <= self.thresholds, expected <= self.thresholds)):
            raise ValueError("ONNX не воспроизводит контрольные прогнозы и голоса")

    def predict(self, window):
        if (window.ndim != 3 or window.shape[1:] != (WINDOW, len(FEATURES))
                or window.dtype != np.float64 or not np.isfinite(window).all()):
            raise ValueError("Неверная форма или тип окна")
        scores = np.column_stack([item.run(["risk"], {"features": window})[0] for item in self.sessions])
        if scores.shape != (len(window), len(SEEDS)) or not np.isfinite(scores).all() or (scores < 0).any():
            raise ValueError("Некорректные ONNX-прогнозы")
        return scores

    def decide(self, window):
        scores = self.predict(window)[0]
        votes = scores <= self.thresholds
        direction = int(window[0, -1, -1])
        return dict(direction=direction, side=direction if votes.sum() >= 3 else 0,
                    scores=scores.tolist(), votes=votes.tolist(), thresholds=self.thresholds.tolist(),
                    model=self.identity, valid_until=self.end.isoformat())


class Models:
    def __init__(self, path):
        self.path = path
        self.active = None

    def for_day(self, day):
        try:
            manifest = (self.path / "manifest.json").read_bytes()
            if self.active is None or self.active.identity != asset_digest(manifest):
                candidate = ModelBundle(self.path, manifest)
                if candidate.start <= day < candidate.end:
                    self.active = candidate
        except (OSError, ValueError, KeyError, RuntimeError, *ORT_ERRORS) as error:
            if self.active is None or not self.active.start <= day < self.active.end:
                raise ValueError(f"Комплект моделей недоступен: {error}") from error
        if self.active is None or not self.active.start <= day < self.active.end:
            raise ValueError("Нет проверенного комплекта на текущие сутки")
        return self.active
