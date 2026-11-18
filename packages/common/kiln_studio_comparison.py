"""Compare saved temperature predictions and a mechanism scenario at one origin."""

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from math import floor
from statistics import median

from common.kiln_studio_engine import target_key
from common.kiln_studio_evaluation import error_statistics
from common.kiln_studio_forecast import _adjustment, _alerts
from common.kiln_studio_observations import clean_rows
from common.kiln_studio_replay import (
    _HELD_SIGNALS,
    _mechanism_points,
    _plan_at,
    _recursive_points,
    temperature_trajectory_method,
)
from common.kiln_studio_schema import ExperimentRequest, TemperatureReport

_EPSILON = 1e-7


def _paired_statistics(points, left, right):
    return error_statistics([p[left] for p in points], [p[right] for p in points])


def compare_temperature_plans(cycle, artifact, request, zone_count, *, thermal_enabled):
    result = _compare(cycle, artifact, request, zone_count, thermal_enabled=thermal_enabled)
    if request.excludedRange is None:
        return result

    original = _compare(
        cycle,
        artifact,
        request.model_copy(update={"excludedRange": None}),
        zone_count,
        thermal_enabled=thermal_enabled,
    )
    original_points = {point["timestamp"]: point for point in original["points"]}
    differences = []
    for point in result["points"]:
        before = original_points.get(point["timestamp"])
        if before is None or point["minute"] <= result["originMinute"]:
            continue
        point["beforeExclusionAlgorithm"] = before["algorithmForecast"]
        point["beforeExclusionMechanism"] = before["forecast"]
        for series in ("algorithmForecast", "forecast"):
            if point[series] is not None and before[series] is not None:
                differences.append(abs(point[series] - before[series]))
    comparison = result["comparison"]
    comparison["exclusionMaxDeltaC"] = max(differences, default=None)
    comparison["exclusionComparableCount"] = len(differences)
    return result


def _compare(cycle, artifact, request, zone_count, *, thermal_enabled):
    config = ExperimentRequest.model_validate(artifact["config"])
    if config.task != "temperature":
        raise ValueError("仅温度模型可运行温度对比")
    physics_model = artifact["algorithm"] == "physics"
    trajectory = temperature_trajectory_method(artifact, config)
    mechanism_enabled = physics_model or thermal_enabled
    adjusted = bool(request.offsetC or request.tauMinutes is not None)
    if adjusted and not mechanism_enabled:
        raise ValueError("当前版本未启用温度机理，无法计算调整方案")

    rows = clean_rows(
        cycle["points"], zone_count, exclude_zero_temperature=config.excludeZeroTemperatureRecords
    )
    times = [row["time"] for row in rows]
    if len(times) < 2:
        raise ValueError("该工况记录不足两个采样点")
    gaps = [right - left for left, right in zip(times, times[1:], strict=False)]
    if min(gaps) <= 0:
        raise ValueError("记录时间必须严格递增")
    max_gap = median(gaps) * 1.5 + _EPSILON
    cursor = times[0] + (times[-1] - times[0]) * request.cursorFraction
    start = bisect_right(times, cursor + _EPSILON) - 1
    origin = rows[start]
    origin_time = times[start]
    key = target_key(config)
    if origin[key] is None:
        raise ValueError("预测起点缺少实测温度，请移动预测起点")
    first, stop = 0, len(rows)
    excluded_first = excluded_stop = len(rows)
    if request.excludedRange is not None:
        exclude_start, exclude_end = request.excludedRange.bounds()
        excluded_first = bisect_left(times, exclude_start)
        excluded_stop = bisect_left(times, exclude_end)
        if excluded_first == excluded_stop:
            raise ValueError("排除区间内没有历史记录，请检查所选时间")
        if excluded_first <= start < excluded_stop:
            raise ValueError("预测起点落在排除区间内，请移动起点")
        if start >= excluded_stop:
            first = excluded_stop
            if rows[first].get("gas_total") is not None:
                rows[first].update(gas=None, afr=None)
        else:
            stop = excluded_first

    # Keep the archive clock. Limit forecasts to a continuous segment from the origin.
    plans = [row["temp_sp"] for row in rows[:stop]]
    future_times = times[:stop]
    gap = next((index for index in range(start, stop - 1) if gaps[index] > max_gap), stop - 1)
    available = min(request.durationMinutes, floor(times[gap] - origin_time + _EPSILON))
    future_times = future_times[: gap + 1]
    plans = plans[: gap + 1]
    if available <= 0:
        raise ValueError("起点后没有足够的连续记录，请更换预测起点")

    notes = []
    if available < request.durationMinutes:
        notes.append(f"连续记录只支持起点后 {available} 分钟，后续保持空白。")
    if request.offsetC and available <= request.delayMinutes:
        notes.append("调整尚未进入本次可计算窗口。")
    mechanism, scenario, algorithm = {}, {}, {}
    saved_tau = artifact["parameters"].get("tauMinutes") if mechanism_enabled else None
    if saved_tau is not None:
        mechanism = _mechanism_points(
            origin[key], origin_time, available, future_times, plans, max_gap, saved_tau
        )
        if len(mechanism) < available:
            notes.append("后续设定计划缺测，机理曲线在最后可计算的时刻结束。")
        if adjusted and mechanism:
            scenario = _mechanism_points(
                origin[key],
                origin_time,
                max(mechanism),
                future_times,
                plans,
                max_gap,
                request.tauMinutes or saved_tau,
                scenario=request,
            )
        elif adjusted:
            raise ValueError("起点后缺少连续设定计划，无法计算调整方案")
    if not physics_model:
        # Restart history after a sampling interruption as well as an explicit exclusion.
        invalid_gap = next((i for i in range(start - 1, first - 1, -1) if gaps[i] > max_gap), None)
        if invalid_gap is not None:
            first = invalid_gap + 1
        history_start = (
            bisect_right(times, origin_time - config.historyMinutes, first, start + 1) - 1
        )
        if history_start < first:
            notes.append(f"起点前可用历史不足 {config.historyMinutes} 分钟，算法预测留空。")
        else:
            held = {}
            for signal in _HELD_SIGNALS:
                held[signal] = next(
                    (
                        row[signal]
                        for row in reversed(rows[first : start + 1])
                        if row[signal] is not None
                    ),
                    None,
                )
            path, _ = _recursive_points(
                rows[history_start : start + 1],
                held,
                artifact,
                config,
                key,
                available,
                future_times,
                plans,
                max_gap,
            )
            algorithm = {
                item["minuteOffset"]: item["value"] for item in path if item["value"] is not None
            }
            if not algorithm:
                notes.append(
                    f"当前窗口没有该算法可计算的点。模型训练时距为 {config.horizonMinutes} 分钟。"
                )
            elif trajectory == "persistence":
                notes.append("持续值基线逐分钟沿用起点实测温度，作为变化趋势的对照。")
            elif max(algorithm) > config.horizonMinutes:
                notes.append(f"超过训练时距 {config.horizonMinutes} 分钟的算法走势来自递推。")

    if not (mechanism or algorithm):
        raise ValueError("当前起点和窗口无法生成预测，请更换起点或增大窗口")
    end_time = origin_time + request.durationMinutes
    view_stop = bisect_right(times, end_time + _EPSILON, start + 1)
    original = mechanism or algorithm
    computed_offsets = sorted({*mechanism, *scenario, *algorithm})
    origin_date = datetime.fromisoformat(origin["ts"])
    points = []

    def append_point(time, row, *, excluded=False, offset=None):
        past = time <= origin_time + _EPSILON
        plan = row.get("temp_sp") if row else _plan_at(future_times, plans, time, max_gap)
        measured = row.get(key) if row and not excluded else None
        is_origin = offset == 0
        points.append(
            {
                "minute": time - times[0],
                "recordPoint": row is not None,
                "timestamp": row["ts"]
                if row
                else (origin_date + timedelta(minutes=offset)).isoformat(),
                "actual": measured if past else None,
                "actualAfter": measured if not past else None,
                "setpoint": None if excluded else plan,
                "adjustedSetpoint": plan + _adjustment(offset, request)
                if plan is not None and (offset in scenario or is_origin and scenario)
                else None,
                "forecast": origin[key] if is_origin and mechanism else mechanism.get(offset),
                "algorithmForecast": algorithm.get(offset),
                "scenario": origin[key] if is_origin and scenario else scenario.get(offset),
                "rolling": None,
                "gas": row.get("gas") if row and past and not excluded else None,
                "air": row.get("air") if row and past and not excluded else None,
                "pressure": row.get("pressure") if row and past and not excluded else None,
                "oxygen": row.get("o2") if row and past and not excluded else None,
                "zoneTemperatures": [row.get(f"z{zone}_temp") for zone in range(1, zone_count + 1)]
                if row and past and not excluded
                else [],
            }
        )

    next_offset = iter(computed_offsets)
    offset = next(next_offset, None)
    for index, time in enumerate(times[:view_stop]):
        while offset is not None and origin_time + offset < time - _EPSILON:
            append_point(origin_time + offset, None, offset=offset)
            offset = next(next_offset, None)
        aligned = offset is not None and abs(origin_time + offset - time) <= _EPSILON
        append_point(
            time,
            rows[index],
            excluded=excluded_first <= index < excluded_stop,
            offset=0 if index == start else offset if aligned else None,
        )
        if aligned:
            offset = next(next_offset, None)
    while offset is not None:
        append_point(origin_time + offset, None, offset=offset)
        offset = next(next_offset, None)

    actual_points = [point for point in points if point["actualAfter"] is not None]
    common = [
        point
        for point in actual_points
        if point["forecast"] is not None and point["algorithmForecast"] is not None
    ]
    end_offset = max(original)
    baseline_end, adjusted_end = original[end_offset], scenario.get(end_offset)
    max_change_at = max(
        scenario, key=lambda offset: abs(scenario[offset] - original[offset]), default=None
    )
    max_change = (
        scenario[max_change_at] - original[max_change_at] if max_change_at is not None else None
    )
    target = request.targetC
    target_times = []
    for values in (original, scenario):
        reached = None
        if values and target is not None:
            if origin[key] >= target:
                reached = 0
            else:
                reached = next(
                    (offset for offset, value in values.items() if value >= target), None
                )
        target_times.append(reached)

    alerts = _alerts(
        points,
        "forecast",
        "setpoint",
        request.deviationC,
        request.deviationMinutes,
        max_gap,
        "原计划机理",
    )
    alerts += _alerts(
        points,
        "scenario",
        "adjustedSetpoint",
        request.deviationC,
        request.deviationMinutes,
        max_gap,
        "调整方案机理",
    )
    return {
        "mode": "comparison",
        "cycleId": cycle["id"],
        "cycleName": cycle["name"],
        "zone": config.zone,
        "targetLabel": f"{config.zone} 区" if config.zone else "有效温区平均",
        "algorithm": artifact["algorithm"],
        "originAt": origin["ts"],
        "originMinute": origin_time - times[0],
        "scope": "按起点观测与档案设定计划计算。事后实测用于核对原计划。",
        "request": request.model_dump(),
        "points": points,
        "alerts": alerts,
        "notes": notes,
        "metrics": {
            "pointCount": len(original),
            "endAt": (origin_date + timedelta(minutes=end_offset)).isoformat(),
            "endTemperature": baseline_end,
            "scenarioEndTemperature": adjusted_end,
            "peakTemperature": max(original.values()),
            "savedTauMinutes": saved_tau,
            "scenarioTauMinutes": request.tauMinutes or saved_tau if scenario else None,
            "trainedHorizonMinutes": config.horizonMinutes,
        },
        "comparison": {
            "mechanismEnabled": mechanism_enabled,
            "originTemperature": origin[key],
            "originSetpoint": origin["temp_sp"],
            "coveredMinutes": end_offset,
            "algorithmCoveredMinutes": max(algorithm, default=0),
            "algorithmPoints": len(algorithm),
            "algorithmTrajectory": trajectory,
            "endDeltaC": adjusted_end - baseline_end if adjusted_end is not None else None,
            "maxChangeC": max_change,
            "maxChangeMinutes": max_change_at,
            "scenarioPeakTemperature": max(scenario.values(), default=None),
            "targetMinutes": target_times[0],
            "scenarioTargetMinutes": target_times[1],
            "mechanismActual": _paired_statistics(actual_points, "actualAfter", "forecast"),
            "algorithmActual": _paired_statistics(
                actual_points, "actualAfter", "algorithmForecast"
            ),
            "commonMechanismActual": _paired_statistics(common, "actualAfter", "forecast"),
            "commonAlgorithmActual": _paired_statistics(common, "actualAfter", "algorithmForecast"),
            "excludedCount": excluded_stop - excluded_first,
        },
    }


def temperature_comparison_report(result):
    comparison = result["comparison"]
    request = result["request"]
    name = comparison.get("algorithmName", result["algorithm"])
    delta = comparison["endDeltaC"]
    findings = []
    if delta is not None:
        change = "基本持平" if round(delta, 2) == 0 else f"变化 {delta:+.2f} ℃"
        terminal = (
            f"起点后第 {comparison['coveredMinutes']} 分钟，调整方案的机理预测"
            f"较原计划{change}。原计划 {result['metrics']['endTemperature']:.2f} ℃，"
            f"调整后 {result['metrics']['scenarioEndTemperature']:.2f} ℃。"
        )
        maximum = comparison.get("maxChangeC")
        if maximum is not None and round(maximum, 2) != 0:
            terminal += (
                f"窗口内最大温差为 {maximum:+.2f} ℃，"
                f"出现在起点后第 {comparison['maxChangeMinutes']} 分钟。"
            )
        findings.append(terminal)
    else:
        findings.append(
            f"原计划预测至起点后第 {comparison['coveredMinutes']} 分钟，"
            f"终点温度 {result['metrics']['endTemperature']:.2f} ℃。"
        )
    if request.get("targetC") is not None:

        def arrival(minutes):
            return "计算范围内未达到" if minutes is None else f"起点后约 {minutes} 分钟"

        target = f"目标温度 {request['targetC']:g} ℃。原计划{arrival(comparison['targetMinutes'])}"
        if delta is not None:
            target += f"，调整方案{arrival(comparison['scenarioTargetMinutes'])}"
        findings.append(target + "。按预测点判断。")
    a, m = comparison["commonAlgorithmActual"], comparison["commonMechanismActual"]
    if a["count"]:
        delta = a["mae"] - m["mae"]
        advantage = (
            "两者在本窗口的平均相差接近。"
            if round(delta, 2) == 0
            else (f"{name}平均相差比机理{'小' if delta < 0 else '大'} {abs(delta):.2f} ℃。")
        )
        findings.append(
            f"在本窗口 {a['count']} 个共同实测时刻，{name}平均相差 {a['mae']:.2f} ℃，"
            f"机理平均相差 {m['mae']:.2f} ℃。{advantage}"
        )
    elif comparison["algorithmActual"]["count"] or comparison["mechanismActual"]["count"]:
        score = (
            comparison["algorithmActual"]
            if comparison["algorithmActual"]["count"]
            else comparison["mechanismActual"]
        )
        label = name if comparison["algorithmActual"]["count"] else "机理预测"
        findings.append(
            f"本窗口 {score['count']} 个相同时间的实测点中，{label}平均相差 {score['mae']:.2f} ℃。"
        )
    else:
        findings.append("本窗口没有和预测对齐的事后实测，现场误差待核对。")
    test = comparison.get("historicalTest") or {}
    if test.get("mae") is not None and test.get("rmse") is not None:
        findings.append(
            f"{name}保存的提前 {result['metrics']['trainedHorizonMinutes']} 分钟测试结果"
            f"为平均相差 {test['mae']:.2f} ℃、综合温差 {test['rmse']:.2f} ℃，"
            f"共 {test['count']} 个有效点。"
        )
    if comparison["excludedCount"]:
        max_delta = comparison.get("exclusionMaxDeltaC")
        effect = (
            "排除前后没有共同可计算的预测点。"
            if max_delta is None
            else f"排除前后共同可计算的预测点中，最大变化为 {max_delta:.2f} ℃。"
        )
        findings.append(f"本次回放排除了 {comparison['excludedCount']} 条观测。{effect}")
    limitations = ["调整方案仅有试算结果。事后实测对应原计划。"]
    if comparison.get("afterTraining") is False:
        limitations.append("起点位于训练时间范围内，本窗口误差仅用于演示。")
    limitations.extend(result["notes"][: 5 - len(limitations)])
    return TemperatureReport(
        summary=f"{result['targetLabel']} · {name} · 未来 {request['durationMinutes']} 分钟对比",
        findings=findings,
        limitations=limitations,
    )
