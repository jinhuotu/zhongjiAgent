"""训练与回放共用的测点缺测规则，保留时间轴和原记录。"""

import re
from math import ceil, isfinite
from statistics import median

SIGNALS = ("temp", "temp_sp", "gas", "air", "afr", "o2", "pressure")


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
        row = {**point, **{key: observed(point.get(key)) for key in signals}}
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


def observation_quality(cycles, exclude_zero):
    sentinel = zeros = 0
    for cycle in cycles:
        zones = inferred_zones(cycle["points"])
        for row in cycle["points"]:
            sentinel += any(
                value == -9999 for value in row.values() if isinstance(value, int | float)
            )
            zeros += zero_temperature_row(row, zones)
    return {
        "sentinelRows": sentinel,
        "zeroTemperatureRows": zeros,
        "excludeZeroTemperatureRecords": exclude_zero,
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
