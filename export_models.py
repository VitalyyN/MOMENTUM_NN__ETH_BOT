"""Экспорт на основном ПК из проверенного пакета ноутбука или месячного состояния."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from uuid import uuid4

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
import pandas as pd
import torch

from market import HISTORY_DAYS, SEEDS, feature_window, read_hours
from models import ModelBundle, asset_digest
from settings import ROOT


def graph(weights, scale):
    """ONNX float64 с теми же слоями и масштабированием, что в RobotRisk."""
    constants = {
        "mean": np.r_[scale["mean"], 0., 0., 0.],
        "scale": np.r_[scale["scale"], 1., 1., 1.],
        "target_scale": scale["target_scale"],
        "w1": weights["net.0.weight"].numpy().T,
        "b1": weights["net.0.bias"].numpy(),
        "w2": weights["net.2.weight"].numpy().T,
        "b2": weights["net.2.bias"].numpy(),
        "zero": 0., "one": 1., "cutoff": 20.,
    }
    nodes = [
        ("Sub", ["features", "mean"], "centered"),
        ("Div", ["centered", "scale"], "scaled"),
        ("Flatten", ["scaled"], "flat"),
        ("Gemm", ["flat", "w1", "b1"], "hidden"),
        ("Tanh", ["hidden"], "activated"),
        ("Gemm", ["activated", "w2", "b2"], "z"),
        ("Abs", ["z"], "abs"),
        ("Neg", ["abs"], "negative"),
        ("Exp", ["negative"], "exp"),
        ("Add", ["exp", "one"], "sum"),
        ("Log", ["sum"], "log"),
        ("Max", ["z", "zero"], "positive"),
        ("Add", ["positive", "log"], "softplus"),
        ("Greater", ["z", "cutoff"], "linear"),
        ("Where", ["linear", "z", "softplus"], "score"),
        ("Mul", ["score", "target_scale"], "scaled_score"),
        ("Flatten", ["scaled_score"], "matrix"),
    ]
    initializers = [numpy_helper.from_array(np.asarray(value, dtype=np.float64), name)
                    for name, value in constants.items()]
    initializers.append(numpy_helper.from_array(np.array([1], dtype=np.int64), "axis"))
    nodes.append(("Squeeze", ["matrix", "axis"], "risk"))
    model = helper.make_model(
        helper.make_graph(
            [helper.make_node(op, args, [output]) for op, args, output in nodes],
            "ETHUSDT_MLP2_F13_5D",
            [helper.make_tensor_value_info("features", TensorProto.DOUBLE, ["batch", 30, 13])],
            [helper.make_tensor_value_info("risk", TensorProto.DOUBLE, ["batch"])],
            initializers,
        ),
        opset_imports=[helper.make_opsetid("", 13)], ir_version=8,
    )
    onnx.checker.check_model(model)
    return model.SerializeToString()


def export(source, folder, history=()):
    meta = json.loads((source / "manifest.json").read_bytes())
    for name, expected in meta["SHA256"].items():
        if expected != asset_digest((source / name).read_bytes()):
            raise ValueError(f"Изменён исходный артефакт: {name}")
    smoke = source / "smoke_window.npz"
    shutil.copyfile(smoke, folder / smoke.name)
    with np.load(smoke, allow_pickle=False) as data:
        windows = [data["F13"][0]]
    if history:
        candles = pd.concat([read_hours(path) for path in history])
        market_end = pd.Timestamp(meta["MarketEndExclusive"])
        first = max(pd.Timestamp(meta["ModelAsOf"]) - pd.Timedelta(days=365),
                    candles.index[0].ceil("D") + pd.Timedelta(days=HISTORY_DAYS))
        end = min(market_end, (candles.index[-1] + pd.Timedelta(hours=1)).floor("D"))
        if first > end:
            raise ValueError("Для сверки не хватает 130 полных дней истории")
        windows.extend(feature_window(candles, day)[0] for day in pd.date_range(first, end, freq="D"))
    windows = np.stack(windows)
    references = []
    for seed in SEEDS:
        checkpoint = source / str(seed)
        weights = torch.load(checkpoint / "best.pth", map_location="cpu", weights_only=True)
        with np.load(checkpoint / "scaler.npz", allow_pickle=False) as scale:
            (folder / f"{seed}.onnx").write_bytes(graph(weights, scale))
        with (checkpoint / "risk_model.pt").open("rb") as stream, torch.inference_mode():
            reference = torch.jit.load(stream, map_location="cpu").eval()
            references.append(reference(torch.from_numpy(windows)).numpy())
    keys = ("Instrument", "Features", "Seeds", "InputShape", "InputDtype", "RiskQuantile",
            "RiskTargetDays", "CalibrationQuantile", "VotesNeeded", "Thresholds",
            "ModelAsOf", "ModelValidUntilExclusive", "MarketEndExclusive")
    manifest = {key: meta[key] for key in keys}
    manifest["SourceManifestSHA256"] = asset_digest((source / "manifest.json").read_bytes())
    manifest["SHA256"] = {path.name: asset_digest(path.read_bytes()) for path in sorted(folder.iterdir())}
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    bundle = ModelBundle(folder, raw)
    expected = np.column_stack(references)
    predicted = bundle.predict(windows)
    np.testing.assert_allclose(predicted, expected, atol=1e-12, rtol=1e-10)
    np.testing.assert_array_equal(predicted <= bundle.thresholds, expected <= bundle.thresholds)
    (folder / "manifest.json").write_bytes(raw)
    print(f"Сверены {len(windows)} окон × {len(SEEDS)} моделей. "
          f"Максимальная ошибка: {np.max(np.abs(predicted - expected)):.3g}. "
          f"Ближайший прогноз к порогу: {np.min(np.abs(expected - bundle.thresholds)):.3g}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Каталог robot_bundle с весами и манифестом")
    parser.add_argument("--history", type=Path, nargs="+", help="Последовательные CSV часовых свечей")
    args = parser.parse_args()
    dest = ROOT / "models" / "current"
    dest.parent.mkdir(exist_ok=True)
    pending = dest.parent / f".export-{uuid4().hex}"
    pending.mkdir()
    try:
        export(args.source, pending, args.history)
        if dest.exists():
            old = json.loads((dest / "manifest.json").read_bytes())
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive = ROOT / "архив" / old["ModelAsOf"][:7] / stamp
            archive.parent.mkdir(parents=True, exist_ok=True)
            dest.rename(archive)
            try:
                pending.rename(dest)
            except OSError:
                archive.rename(dest)
                raise
        else:
            pending.rename(dest)
    except Exception:
        # Диагностика частичного экспорта остаётся на основном ПК.
        print(f"Не завершён экспорт: {pending}")
        raise


if __name__ == "__main__":
    main()
