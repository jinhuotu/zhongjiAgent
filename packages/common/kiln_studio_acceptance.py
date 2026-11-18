"""可复核的温度事件与按自然时间段积分的燃气验收。"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median

from common.kiln_studio_evaluation import error_statistics

LOCAL = timezone(timedelta(hours=8))
EPSILON = 1e-6


def minute(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL)
    return parsed.timestamp() / 60


def timestamp(value):
    return datetime.fromtimestamp(value * 60, LOCAL).isoformat(timespec="seconds")


def _groups(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["cycle"]].append(row)
    return {
        key: sorted(values, key=lambda row: minute(row["ts"])) for key, values in groups.items()
    }


def nominal_gaps(rows):
    result = {}
    for key, group in _groups(rows).items():
        times = [minute(row["ts"]) for row in group]
        gaps = [right - left for left, right in zip(times, times[1:], strict=False) if right > left]
        result[key] = median(gaps) * 1.5 if gaps else 0
    return result


def interval_evaluation(rows, algorithm, specification):
    if not specification:
        return None
    radius = specification["radius"]
    pairs = [(row["actual"], row.get(algorithm)) for row in rows if row.get(algorithm) is not None]
    count = len(pairs)
    covered = sum(abs(actual - predicted) <= radius for actual, predicted in pairs)
    return {
        **specification,
        "count": count,
        "coveredCount": covered,
        "coverage": covered / count if count else None,
        "meetsTarget": covered / count >= 0.9 if count else None,
    }


def _period_floor(value, length):
    offset = 480 if length == 1440 else 0
    return (value + offset) // length * length - offset


def _integral(v0, v1, start, end, lo, hi):
    if v0 is None or v1 is None or not isfinite(v0) or not isfinite(v1):
        return None
    slope = (v1 - v0) / (end - start)
    return ((v0 + slope * (lo - start)) + (v0 + slope * (hi - start))) * (hi - lo) / 120


def energy_periods(rows, algorithm, max_gaps=None):
    gaps = max_gaps if max_gaps is not None else nominal_gaps(rows)
    groups = _groups(rows)
    times = [minute(row["ts"]) for row in rows]
    if not times:
        return {"hourly": _period_result([], 12), "daily": _period_result([], 8)}
    results = {}
    for name, length, threshold in (("hourly", 60, 12), ("daily", 1440, 8)):
        first = _period_floor(min(times), length)
        last = _period_floor(max(times) - EPSILON, length) if max(times) > min(times) else first
        buckets = {start: [] for start in range(int(first), int(last) + length, length)}
        for cycle, group in groups.items():
            for left, right in zip(group, group[1:], strict=False):
                start, end = minute(left["ts"]), minute(right["ts"])
                if not 0 < end - start <= gaps[cycle] + EPSILON:
                    continue
                a0, a1 = left["actual"], right["actual"]
                p0, p1 = left.get(algorithm), right.get(algorithm)
                for bucket_start in range(
                    int(_period_floor(start, length)),
                    int(_period_floor(end, length)) + length,
                    length,
                ):
                    lo, hi = max(start, bucket_start), min(end, bucket_start + length)
                    if hi <= lo + EPSILON:
                        continue

                    buckets.setdefault(bucket_start, []).append(
                        (
                            lo,
                            hi,
                            _integral(a0, a1, start, end, lo, hi),
                            _integral(p0, p1, start, end, lo, hi),
                        )
                    )
        periods = []
        for start, segments in sorted(buckets.items()):
            covered_actual = covered_prediction = actual = prediction = 0.0
            overlap = False
            previous_end = start
            for lo, hi, actual_volume, predicted_volume in sorted(
                segments, key=lambda item: item[:2]
            ):
                if lo < previous_end - EPSILON:
                    overlap = True
                previous_end = max(previous_end, hi)
                if actual_volume is not None:
                    actual += actual_volume
                    covered_actual += hi - lo
                if predicted_volume is not None:
                    prediction += predicted_volume
                    covered_prediction += hi - lo
            complete = (
                not overlap
                and abs(covered_actual - length) < EPSILON
                and abs(covered_prediction - length) < EPSILON
            )
            periods.append(
                {
                    "start": timestamp(start),
                    "end": timestamp(start + length),
                    "actual": actual
                    if not overlap and abs(covered_actual - length) < EPSILON
                    else None,
                    "prediction": prediction
                    if not overlap and abs(covered_prediction - length) < EPSILON
                    else None,
                    "coveredMinutes": min(length, covered_actual, covered_prediction),
                    "complete": complete,
                    "reason": "同时段记录重叠" if overlap else "" if complete else "未覆盖完整时段",
                }
            )
        results[name] = _period_result(periods, threshold)
    return results


def _period_result(periods, threshold):
    complete = [row for row in periods if row["complete"]]
    score = error_statistics(
        [row["actual"] for row in complete],
        [row["prediction"] for row in complete],
        mape_min_actual=0.001,
    )
    return {
        "thresholdPct": threshold,
        "metrics": score,
        "periodCount": len(periods),
        "completeCount": len(complete),
        "incompleteCount": len(periods) - len(complete),
        "meetsTarget": score["mape"] <= threshold if score["mape"] is not None else None,
        "periods": periods,
    }


def _threshold_events(group, algorithm, threshold, duration, max_gap, *, observed):
    events, active = [], []
    left_censored = False
    previous_time = None

    def finish():
        if active and active[-1]["at"] - active[0]["at"] + EPSILON >= duration:
            events.append(
                {
                    "start": active[0]["at"],
                    "end": active[-1]["at"],
                    "issued": active[0]["issued"],
                    "peak": max(point["value"] for point in active),
                    "leftCensored": left_censored,
                }
            )

    for row in group:
        at = minute(row["ts"])
        value = row["actual"] if observed else row.get(algorithm)
        continuous = previous_time is not None and at - previous_time <= max_gap + EPSILON
        if active and not continuous:
            finish()
            active = []
        if value is not None and value >= threshold:
            if not active:
                left_censored = observed and not continuous
            active.append({"at": at, "issued": minute(row["origin"]), "value": value})
        elif active:
            finish()
            active = []
        # 缺测后的首次超限无法确定真实起点，保留作待核对事件。
        previous_time = at if value is not None else None
    finish()
    return events


def overtemperature_evaluation(rows, algorithm, threshold, duration=1, tolerance=2, max_gaps=None):
    if threshold is None:
        return None
    gaps = max_gaps if max_gaps is not None else nominal_gaps(rows)
    actual, alerts, censored = [], [], []
    for cycle, group in _groups(rows).items():
        observed = _threshold_events(
            group, algorithm, threshold, duration, gaps[cycle], observed=True
        )
        predicted = _threshold_events(
            group, algorithm, threshold, duration, gaps[cycle], observed=False
        )
        actual.extend({**event, "cycle": cycle} for event in observed if not event["leftCensored"])
        censored.extend({**event, "cycle": cycle} for event in observed if event["leftCensored"])
        alerts.extend({**event, "cycle": cycle} for event in predicted)
    matched_alerts = set()
    details = []
    early = matched = 0
    for event in sorted(actual, key=lambda item: item["start"]):
        candidates = [
            (abs(alert["start"] - event["start"]), index, alert)
            for index, alert in enumerate(alerts)
            if index not in matched_alerts
            and alert["cycle"] == event["cycle"]
            and abs(alert["start"] - event["start"]) <= tolerance + EPSILON
            and alert["issued"] <= event["start"] + EPSILON
        ]
        best = min(candidates, key=lambda item: item[0]) if candidates else None
        lead = event["start"] - best[2]["issued"] if best else None
        if best:
            matched_alerts.add(best[1])
            matched += 1
            early += lead >= 5 - EPSILON
        details.append(
            {
                "cycle": event["cycle"],
                "actualStart": timestamp(event["start"]),
                "actualEnd": timestamp(event["end"]),
                "alertAt": timestamp(best[2]["issued"]) if best else None,
                "leadMinutes": lead,
                "timely": lead >= 5 - EPSILON if lead is not None else False,
                "peak": event["peak"],
            }
        )
    false_alerts = [alert for index, alert in enumerate(alerts) if index not in matched_alerts]
    # 真实起点被截断的事件附近提醒保留为待核对，避免直接计作误报。
    pending = [
        alert
        for alert in false_alerts
        if any(
            event["cycle"] == alert["cycle"]
            and alert["start"] <= event["end"] + tolerance
            and alert["end"] >= event["start"] - tolerance
            for event in censored
        )
    ]
    scored_alerts = len(alerts) - len(pending)
    false_count = len(false_alerts) - len(pending)
    return {
        "thresholdC": threshold,
        "durationMinutes": duration,
        "toleranceMinutes": tolerance,
        "minimumLeadMinutes": 5,
        "actualCount": len(actual),
        "alertCount": scored_alerts,
        "timelyCount": early,
        "lateCount": matched - early,
        "missedCount": len(actual) - matched,
        "falseAlertCount": false_count,
        "pendingActualCount": len(censored),
        "pendingAlertCount": len(pending),
        "hitRate": early / len(actual) if actual else None,
        "falseAlertRate": false_count / scored_alerts if scored_alerts else None,
        "hitTarget": early / len(actual) >= 0.85 if actual else None,
        "falseAlertTarget": false_count / scored_alerts <= 0.1 if scored_alerts else None,
        "events": details,
    }
