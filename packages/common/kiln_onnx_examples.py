"""ONNX 一致性核对使用绑定数据的分散历史窗口，未来只读取已知温度计划。"""

from bisect import bisect_left, bisect_right

import numpy as np

from common.errors import AppError, ErrorCode
from common.kiln_onnx_graph import implementation, output_offsets
from common.kiln_studio_engine import (
    algorithm_implementation,
    make_features,
    predict_model,
    predict_path,
)
from common.kiln_studio_observations import clean_rows
from common.kiln_studio_schema import ExperimentRequest
from common.kiln_studio_temperature_path import horizon_features, predict_temperature_head


def reference_values(model, ex, plans):
    offsets = output_offsets(model)
    impl = algorithm_implementation(implementation(model))
    if impl and impl.predict_path:
        path = predict_path(model, ex)
        values = [path[minute] for minute in offsets]
    elif len(offsets) > 1:
        values = [
            predict_temperature_head(model, horizon_features(ex, plans[m - 1], m), m)
            if m != model["config"]["horizonMinutes"]
            else predict_model(model, ex)
            for m in offsets
        ]
    else:
        values = [predict_model(model, ex)]
    if any(v is None or not np.isfinite(v) for v in values):
        return None
    return [max(0, v) if model["config"]["task"] in {"energy", "afr"} else v for v in values]


def _cycle_samples(model, cycle, zone_count):
    config = ExperimentRequest.model_validate(model["config"])
    raw = cycle["points"]
    rows = clean_rows(
        raw,
        zone_count,
        model["parameters"]["minGas"] if config.task == "afr" else None,
        exclude_zero_temperature=config.excludeZeroTemperatureRecords,
    )
    if not rows:
        return
    times = [row["time"] for row in rows]
    first = bisect_left(times, times[0] + config.historyMinutes)
    last = bisect_right(times, times[-1] - config.horizonMinutes) - 1
    for index in np.unique(np.linspace(first, last, 8).astype(int)) if last >= first else []:
        origin = rows[index]
        future = [
            bisect_left(times, origin["time"] + m) for m in range(1, config.horizonMinutes + 1)
        ]
        if any(
            j >= len(rows) or times[j] - origin["time"] - m > 5 for m, j in enumerate(future, 1)
        ):
            continue
        plans = [rows[j].get("temp_sp") for j in future]
        impl = algorithm_implementation(implementation(model))
        ex = make_features(
            rows[: index + 1], plans[-1], config, include_sequence=bool(impl and impl.sequence)
        )
        if ex is None:
            continue
        expected = reference_values(model, ex, plans)
        if expected is None:
            continue
        # 多保留一个原始前序点，复现累计表异常对紧邻行的清洗规则。
        start = max(0, bisect_right(times, origin["time"] - config.historyMinutes) - 2)
        yield {
            "origin": ex["origin"],
            "cycleId": cycle["id"],
            "features": dict(zip(model["features"], ex["x"], strict=True)),
            **({"sequence": ex["sequence"]} if "sequence" in ex else {}),
            "futurePlans": plans,
            "history": raw[start : index + 1],
            "expectedValues": expected,
        }


def model_samples(model, cycles, zone_count):
    selected = (
        np.unique(np.linspace(0, len(cycles) - 1, min(6, len(cycles))).astype(int))
        if cycles
        else []
    )
    samples = []
    for index in selected:
        candidates = list(_cycle_samples(model, cycles[index], zone_count))
        if candidates:
            positions = np.unique(
                np.linspace(0, len(candidates) - 1, min(4, len(candidates))).astype(int)
            )
            samples.extend(candidates[position] for position in positions)
    if not samples:
        raise AppError(
            ErrorCode.VALIDATION,
            f"{model['algorithm']} 没有可核对的真实历史窗口，无法导出 ONNX",
            422,
        )
    return samples
