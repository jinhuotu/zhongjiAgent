"""训练与回放共用的测点缺测规则，保留时间轴和原记录。"""

import re
from math import ceil, isfinite
from statistics import median

SIGNALS = ("temp", "temp_sp", "gas", "air", "afr", "o2", "pressure")
# 累计表字段：正常应单调不减，同样需要把 -9999 等非法读数清洗为 None
COUNTER_KEYS = ("gas_total", "air_total")
# 真实重置的最小下降量：实测正常仪表抖动在 0.05 Nm³ 以内，真实重置达数百，1 Nm³ 为稳健分界
RESET_DROP_NM3 = 1.0
# 开窑未清零判定：首个有效读数已超过末值的 30%，说明起点就带着大量累计
NONZERO_START_RATIO = 0.3


def observed(value):
    if isinstance(value, int | float) and not isinstance(value, bool):
        if isfinite(value) and value != -9999:
            return float(value)
    return None


def inferred_zones(points):
    return max(
        (
            int(match[1])
            for key in (points[0] if points else {})
            if (match := re.fullmatch(r"z(\d+)_temp", key))
        ),
        default=0,
    )


def zero_temperature_row(point, zone_count):
    values = [point.get(f"z{zone}_temp") for zone in range(1, zone_count + 1)]
    return point.get("temp_sp") == 0 and (
        all(value == 0 for value in values) if values else point.get("temp") == 0
    )


def clean_rows(points, zone_count=0, afr_floor=None, *, exclude_zero_temperature=False):
    zone_count = max(zone_count, inferred_zones(points))
    signals = (
        *SIGNALS,
        *(
            f"z{zone}_{suffix}"
            for zone in range(1, zone_count + 1)
            for suffix in ("temp", "gas", "air", "air_valve")
        ),
    )
    result = []
    previous = {}
    for point in points:
        row = {
            **point,
            **{key: observed(point.get(key)) for key in signals},
            **{key: observed(point.get(key)) for key in COUNTER_KEYS},
        }
        for suffix in ("temp", "gas", "air"):
            if any(point.get(f"z{zone}_{suffix}") == -9999 for zone in range(1, zone_count + 1)):
                row[suffix] = None
        if point.get("gas_total") == -9999 or previous.get("gas_total") == -9999:
            row["gas"] = None
        if exclude_zero_temperature and zero_temperature_row(point, zone_count):
            row["temp"] = row["temp_sp"] = None
            for zone in range(1, zone_count + 1):
                row[f"z{zone}_temp"] = None
        if row["gas"] is None or row["air"] is None:
            row["afr"] = None
        elif afr_floor is not None:
            row["afr"] = (
                observed(row["air"] / row["gas"])
                if row["gas"] >= afr_floor and row["air"] >= 0
                else None
            )
        result.append(row)
        previous = point
    return result


def counter_health(rows):
    """统计累计表的重置次数与开窑未清零，缺测（None）跳过。

    重置：相邻有效读数的下降量超过 RESET_DROP_NM3（过滤浮点抖动）；
    未清零：首个有效读数已超过末值的 NONZERO_START_RATIO（起点带大量累计）。
    """
    health = {}
    for key in COUNTER_KEYS:
        valid = [row[key] for row in rows if row.get(key) is not None]
        resets = 0
        for left, right in zip(valid, valid[1:], strict=False):
            if left - right > RESET_DROP_NM3:  # 显著下降才判定重置
                resets += 1
        start_value = valid[0] if valid else None
        end_value = valid[-1] if valid else None
        nonzero_start = bool(
            start_value is not None and end_value is not None
            and end_value > 0 and start_value / end_value > NONZERO_START_RATIO
        )
        health[key] = {
            "resetCount": resets,
            "nonzeroStart": nonzero_start,
            "startValue": start_value,
            "endValue": end_value,
        }
    return health


def _freeze_fields(zone_count):
    # 用多个关键测点共同判定，避免单个信号恰好不变就误判
    return ["temp", "gas", "air", *(f"z{zone}_temp" for zone in range(1, zone_count + 1))]


def _is_frozen(left, right, fields):
    # 两行在这些字段上的非空值完全一致，且至少两个字段有值，才算冻结
    compared = 0
    for field in fields:
        lv, rv = left.get(field), right.get(field)
        if lv is None and rv is None:
            continue
        compared += 1
        if lv != rv:
            return False
    return compared >= 2


def freeze_segments(rows, zone_count):
    """检测整帧冻结段：相邻帧多测点完全相同（时间在走、数据不变）。"""
    fields = _freeze_fields(max(zone_count, inferred_zones(rows)))
    segments, start = [], None
    for i in range(1, len(rows)):
        frozen = _is_frozen(rows[i - 1], rows[i], fields)
        if frozen and start is None:
            start = i - 1
        elif not frozen and start is not None:
            segments.append((start, i - 1))
            start = None
    if start is not None:
        segments.append((start, len(rows) - 1))

    def segment_minutes(seg_start, seg_end):
        return round(rows[seg_end]["time"] - rows[seg_start]["time"])

    return {
        "frozenRows": sum(end - begin + 1 for begin, end in segments),
        "longestMinutes": max((segment_minutes(begin, end) for begin, end in segments), default=0),
        "segments": [
            {"start": rows[begin]["ts"], "end": rows[end]["ts"], "minutes": segment_minutes(begin, end)}
            for begin, end in segments
        ],
    }


def observation_quality(cycles, exclude_zero):
    sentinel = zeros = 0
    counter_resets = 0
    nonzero_start = False
    frozen_rows = 0
    longest_freeze = 0
    for cycle in cycles:
        raw_points = cycle["points"]
        zone_count = inferred_zones(raw_points)
        for row in raw_points:
            sentinel += any(
                value == -9999 for value in row.values() if isinstance(value, int | float)
            )
            zeros += zero_temperature_row(row, zone_count)
        # 清洗后再做累计表与冻结检测，原始 points 不做任何删除
        cleaned = clean_rows(raw_points, zone_count)
        counters = counter_health(cleaned)
        counter_resets += counters["gas_total"]["resetCount"]
        nonzero_start = nonzero_start or any(item["nonzeroStart"] for item in counters.values())
        frozen = freeze_segments(cleaned, zone_count)
        frozen_rows += frozen["frozenRows"]
        longest_freeze = max(longest_freeze, frozen["longestMinutes"])
    return {
        "sentinelRows": sentinel,
        "zeroTemperatureRows": zeros,
        "excludeZeroTemperatureRecords": exclude_zero,
        "gasCounterResets": counter_resets,
        "counterNonzeroStart": nonzero_start,
        "frozenRows": frozen_rows,
        "longestFreezeMinutes": longest_freeze,
    }


def zone_observation_trend(points, zone_count):
    rows = clean_rows(points, zone_count)
    keys = [f"z{zone}_temp" for zone in range(1, zone_count + 1)]
    records = [(row["time"], [row[key] for key in keys]) for row in rows]
    gaps = [right[0] - left[0] for left, right in zip(records, records[1:], strict=False)]
    gap_limit = median(gaps) * 1.5 + 1e-6 if gaps else 0
    bucket_size = max(1, ceil(len(records) / 200))
    indices = set()
    for start in range(0, len(records), bucket_size):
        stop = min(start + bucket_size, len(records))
        indices.update((start, stop - 1))
        for zone in range(zone_count):
            valid = [index for index in range(start, stop) if records[index][1][zone] is not None]
            if valid:
                indices.add(min(valid, key=lambda index: records[index][1][zone]))
                indices.add(max(valid, key=lambda index: records[index][1][zone]))
            missing = next((i for i in range(start, stop) if records[i][1][zone] is None), None)
            if missing is not None:
                indices.add(missing)
    gaps_before = [0]
    for gap in gaps:
        gaps_before.append(gaps_before[-1] + (gap > gap_limit))
    trend = []
    previous = None
    for index in sorted(indices):
        if previous is not None and gaps_before[index] != gaps_before[previous]:
            trend.append([(records[previous][0] + records[index][0]) / 2, [None] * zone_count])
        trend.append(records[index])
        previous = index
    return {
        "rowCount": len(rows),
        "displayCount": len(indices),
        "zones": [
            zone + 1 for zone, key in enumerate(keys) if any(row[key] is not None for row in rows)
        ],
        "points": [
            {"timestampMs": round(time * 60_000), "temperatures": values} for time, values in trend
        ],
    }


def process_observations(points, zone_count):
    """Return aligned archive frames, preserving missing channels and actual timestamps."""
    from common.kiln_studio_files import FIELD_NAMES

    keys = [key for key in FIELD_NAMES if key != "ts"] + ["afr"]
    keys.extend(
        f"z{zone}_{suffix}"
        for zone in range(1, zone_count + 1)
        for suffix in ("temp", "gas", "air", "air_valve")
    )
    count = len(points)
    size = min(count, 1800)
    indices = [round(i * (count - 1) / max(1, size - 1)) for i in range(size)]
    gaps = [b["time"] - a["time"] for a, b in zip(points, points[1:], strict=False)]
    limit = median(gaps) * 1.5 + 1e-6 if gaps else 0
    gap_before = [
        any(delta > limit for delta in gaps[a:b])
        for a, b in zip(indices, indices[1:], strict=False)
    ]
    frames = []
    for i in indices:
        # Include the preceding raw counter for the shared missing-reading rules.
        row = clean_rows(points[max(0, i - 1) : i + 1], zone_count)[-1]
        frames.append(
            {
                "time": round(row["time"] * 60000),
                "values": {k: value for k in keys if (value := observed(row.get(k))) is not None},
            }
        )
    return {"rowCount": count, "frames": frames, "gapBefore": [False, *gap_before] if count else []}
