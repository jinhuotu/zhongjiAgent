"""历史特征规则与 kiln-studio-1.0 一致；归一化由 ONNX 图完成。"""

from bisect import bisect_right
from math import isfinite

import numpy as np


def observed(value):
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) if isfinite(value) and value != -9999 else None
    return None


def clean_history(history, spec):
    zones = max(
        spec["zoneCount"],
        max(
            (
                int(k.split("_")[0][1:])
                for k in history[0]
                if k.startswith("z") and k.endswith("_temp") and k[1:-5].isdigit()
            ),
            default=0,
        ),
    )
    signals = ["temp", "temp_sp", "gas", "air", "afr", "o2", "pressure"]
    signals += [
        f"z{z}_{suffix}"
        for z in range(1, zones + 1)
        for suffix in ("temp", "gas", "air", "air_valve")
    ]
    rows, previous = [], {}
    for point in history:
        row = {**point, **{key: observed(point.get(key)) for key in signals}}
        for suffix in ("temp", "gas", "air"):
            if any(point.get(f"z{z}_{suffix}") == -9999 for z in range(1, zones + 1)):
                row[suffix] = None
        if point.get("gas_total") == -9999 or previous.get("gas_total") == -9999:
            row["gas"] = None
        zero = (
            all(point.get(f"z{z}_temp") == 0 for z in range(1, zones + 1))
            if zones
            else point.get("temp") == 0
        )
        if (
            spec["config"].get("excludeZeroTemperatureRecords", False)
            and point.get("temp_sp") == 0
            and zero
        ):
            row["temp"] = row["temp_sp"] = None
            for z in range(1, zones + 1):
                row[f"z{z}_temp"] = None
        if row["gas"] is None or row["air"] is None:
            row["afr"] = None
        elif spec["config"]["task"] == "afr":
            row["afr"] = (
                row["air"] / row["gas"]
                if row["gas"] >= spec["minGas"] and row["air"] >= 0
                else None
            )
        rows.append(row)
        previous = point
    return rows


def from_history(item, spec):
    history = item["history"]
    if not history:
        raise ValueError("history 不能为空")
    rows = clean_history(history, spec)
    times = [row["time"] for row in rows]
    if any(isinstance(t, bool) or not isinstance(t, int | float) or not isfinite(t) for t in times):
        raise ValueError("history.time 必须是有限的分钟数")
    if (
        any(b < a for a, b in zip(times, times[1:], strict=False))
        or len({row.get("cycle") for row in rows}) != 1
    ):
        raise ValueError("history 必须按时间排序且属于同一个窑次")
    cfg, current = spec["config"], rows[-1]
    start = bisect_right(times, times[-1] - cfg["historyMinutes"]) - 1
    if start < 0:
        raise ValueError("历史窗口长度不足")
    window = rows[start:]
    target = spec["targetSignal"]
    values = [row[target] for row in window if row.get(target) is not None]
    if current.get(target) is None or not values:
        raise ValueError("预测起点缺少有效目标读数")
    plans = item.get("futurePlans", [])
    sp1 = (
        plans[cfg["horizonMinutes"] - 1]
        if len(plans) >= cfg["horizonMinutes"]
        else item.get("futureSetpoint")
    )
    sp0 = current.get("temp_sp")
    slope = (
        (sp1 - sp0) * 60 / cfg["horizonMinutes"] if sp0 is not None and sp1 is not None else None
    )
    x = [
        current[target],
        values[0],
        sum(values) / len(values),
        max(values) - min(values),
        current.get("temp"),
        current.get("gas"),
        current.get("air"),
        current.get("o2"),
        current.get("pressure"),
        sp0,
        sp1,
        slope,
    ]
    result = {**item, "features": dict(zip(spec["features"], x, strict=False))}
    if "sequence" in spec:
        indices = np.linspace(0, len(window) - 1, spec["sequence"]["length"]).astype(int)
        result["sequence"] = [
            [window[i].get(key) for key in spec["sequence"]["channels"]] for i in indices
        ]
    return result


def numeric_array(value):
    # 输入允许 null，但不能把字符串或布尔值静默转换成测点数字。
    def check(item):
        if isinstance(item, list):
            for member in item:
                check(member)
        elif item is not None and (isinstance(item, bool) or not isinstance(item, int | float)):
            raise ValueError("测点只允许数字或 null")

    check(value)
    return np.asarray(value, dtype=np.float32)


def prepare(item, spec):
    if "features" not in item:
        item = from_history(item, spec)
    fields = spec["features"]
    missing = [key for key in fields if key not in item["features"]]
    if missing:
        raise ValueError("缺少输入特征：" + "、".join(missing))
    x = numeric_array([item["features"][key] for key in fields])
    if x.shape != (len(fields),) or not np.isfinite(x[0]):
        raise ValueError("特征形状不正确或预测起点目标值无效")
    feeds = {"features": x[None, :]}
    for field in spec["inputs"]:
        key = field["name"]
        if key == "features":
            continue
        value = numeric_array(item[key])
        if list(value.shape) != field["shape"][1:]:
            raise ValueError(key + " 形状必须为 " + str(field["shape"][1:]))
        feeds[key] = value[None, ...]
    return feeds
