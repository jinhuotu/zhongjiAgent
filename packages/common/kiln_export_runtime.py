"""导出用的独立 NumPy 脚本模板；禁止引用平台模块。"""

PYTHON_RUNTIME = r'''"""离线推理：仅依赖 NumPy，按模型原生预测时距计算。"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np


def ordered_features(model, item):
    fields = model["features"]
    values = item["features"]
    missing = [name for name in fields if name not in values]
    if missing:
        raise ValueError("缺少特征：" + "、".join(missing))
    raw = np.asarray([values[name] for name in fields], dtype=float)
    if raw.ndim != 1 or len(raw) != len(fields):
        raise ValueError("每个特征必须是数字或 null")
    return raw


def normalize(raw, prep):
    mean, scale = np.asarray(prep["mean"]), np.asarray(prep["scale"])
    return (np.where(np.isfinite(raw), raw, mean) - mean) / scale


def physics(model, raw):
    p, cfg = model["parameters"], model["config"]
    if cfg["task"] == "afr":
        return p["theoreticalAfr"] * p["excessAir"]
    y0, sp0, sp1, slope = raw[0], raw[9], raw[10], raw[11]
    if not all(math.isfinite(v) for v in (sp0, sp1, slope)):
        return None
    if cfg["task"] == "energy":
        return (p["capacityKJ"] * max(slope, 0)
                + 3600 * p["lossKW"] * max(sp1 - p["ambientC"], 0)) / (
                    1000 * p["efficiency"] * p["lhvMJ"])
    tau, horizon = p["tauMinutes"] / 60, cfg["horizonMinutes"] / 60
    return sp1 - slope * tau + (y0 - sp0 + slope * tau) * math.exp(-horizon / tau)


def lstm(model, raw, item):
    saved = model["model"]
    sequence = np.asarray(item.get("sequence"), dtype=float)
    mean, scale = np.asarray(saved["sequenceMean"]), np.asarray(saved["sequenceScale"])
    expected = (saved["sequenceLength"], len(mean))
    if sequence.shape != expected:
        raise ValueError("sequence 形状必须为 " + str(expected))
    sequence = np.clip((np.where(np.isfinite(sequence), sequence, mean) - mean) / scale, -10, 10)
    context = np.clip(normalize(raw, model["preprocessor"]), -10, 10)[None, :]
    weights = {key: np.asarray(value) for key, value in saved["weights"].items()}
    hidden_size = weights["recurrent"].shape[0]
    hidden = np.zeros((1, hidden_size))
    cell = np.zeros_like(hidden)
    for row in sequence:
        gates = row[None, :] @ weights["input"] + hidden @ weights["recurrent"] + weights["bias"]
        forget, incoming, outgoing, proposal = np.split(gates, 4, axis=1)
        forget, incoming, outgoing = [
            1 / (1 + np.exp(-np.clip(v, -40, 40))) for v in (forget, incoming, outgoing)]
        cell = forget * cell + incoming * np.tanh(proposal)
        hidden = outgoing * np.tanh(cell)
    output = (hidden @ weights["output"].reshape(hidden_size, -1)
              + context @ weights["context"].reshape(len(raw), -1) + weights["offset"])
    values = raw[0] + saved["targetMean"] + saved["targetScale"] * output[0]
    offsets = saved.get("offsets", [model["config"]["horizonMinutes"]])
    horizon = model["config"]["horizonMinutes"]
    return float(values[offsets.index(horizon)]) if horizon in offsets else None


def predict(model, item):
    raw = ordered_features(model, item)
    key = model.get("implementationKey", model["algorithm"])
    if key == "baseline":
        value = raw[0]
    elif key == "physics":
        value = physics(model, raw)
    elif key == "lstm":
        value = lstm(model, raw, item)
    elif key in {"ridge", "lasso", "fusion"}:
        residual = float(np.dot(np.r_[1.0, normalize(raw, model["preprocessor"])], model["model"]))
        base = physics(model, raw) if key == "fusion" else 0
        value = None if base is None else base + residual
    elif key == "boosting":
        x, saved = normalize(raw, model["preprocessor"]), model["model"]
        value = saved["initial"] + saved["rate"] * sum(
            left if x[index] <= threshold else right
            for index, threshold, left, right in saved["trees"])
    else:
        raise ValueError("NumPy 离线包不支持算法：" + key)
    if value is None or not math.isfinite(float(value)):
        return None
    return float(max(0, value)) if model["config"]["task"] in {"energy", "afr"} else float(value)


def run(package, inputs, verify=False):
    results = []
    for item in inputs["inputs"]:
        index = item["modelIndex"]
        if not isinstance(index, int) or not 0 <= index < len(package["models"]):
            raise ValueError("modelIndex 超出模型范围")
        model = package["models"][index]
        value = predict(model, item)
        if verify:
            if "expected" not in item or not np.isclose(
                    value, item["expected"], rtol=1e-9, atol=1e-8):
                raise ValueError("离线结果与平台核对值不一致，modelIndex=" + str(index))
        manifest = package["bundle"]["manifest"][index]
        results.append({"modelIndex": index, "algorithm": model["algorithm"],
                        "task": model["config"]["task"],
                        "horizonMinutes": model["config"]["horizonMinutes"],
                        "unit": manifest["unit"], "value": value})
    return results


def main():
    # Windows 重定向输出也保持 UTF-8，避免 Nm³/h 等单位导致编码失败。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    folder = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="窑炉数值模型离线推理（NumPy）")
    parser.add_argument("--model", type=Path, default=folder / "model.json")
    parser.add_argument("--input", type=Path, default=folder / "input_example.json")
    parser.add_argument("--verify", action="store_true", help="核对输入示例中的平台预测值")
    args = parser.parse_args()
    try:
        package = json.loads(args.model.read_text(encoding="utf-8"))
        inputs = json.loads(args.input.read_text(encoding="utf-8"))
        print(json.dumps(run(package, inputs, args.verify), ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
        parser.exit(1, "推理失败：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
'''
