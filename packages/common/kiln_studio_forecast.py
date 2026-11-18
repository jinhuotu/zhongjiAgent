"""Temperature runs from saved artifacts, with distinct forecast and replay series."""

from bisect import bisect_left, bisect_right
from math import ceil, expm1
from statistics import median

from common.kiln_studio_engine import make_features, predict_model, target_key
from common.kiln_studio_observations import clean_rows
from common.kiln_studio_schema import ExperimentRequest, TemperatureForecastRequest


def _advance(temp, start, finish, minutes, tau):
    # Exact solution for a linear setpoint segment of dT/dt = (Tsp - T) / tau.
    response = -expm1(-minutes / tau)
    return temp + (start - temp) * response + (finish - start) * (1 - tau * response / minutes)


def _adjustment(elapsed, request):
    ramp = (elapsed - request.delayMinutes) / request.rampMinutes
    if request.holdMinutes is not None:
        return_start = request.delayMinutes + request.rampMinutes + request.holdMinutes
        ramp = min(ramp, 1 - (elapsed - return_start) / request.rampMinutes)
    return request.offsetC * min(1, max(0, ramp))


def _advance_scenario(temp, start, finish, elapsed, minutes, tau, request):
    previous = 0
    previous_plan = start + _adjustment(elapsed, request)
    boundaries = [request.delayMinutes, request.delayMinutes + request.rampMinutes]
    if request.holdMinutes is not None:
        return_start = boundaries[-1] + request.holdMinutes
        boundaries.extend((return_start, return_start + request.rampMinutes))
    steps = [t - elapsed for t in boundaries if 0 < t - elapsed < minutes]
    for step in [*steps, minutes]:
        plan = start + (finish - start) * step / minutes + _adjustment(elapsed + step, request)
        if not 0 <= plan <= 1800:
            raise ValueError("调整后的设定超出模型计算范围（0～1800 ℃），请修改偏移")
        temp = _advance(temp, previous_plan, plan, step - previous, tau)
        previous, previous_plan = step, plan
    return temp, previous_plan


def _alerts(points, series, plan, threshold, duration, max_gap, label):
    events, group = [], []
    direction = 0
    for point in [*points, None]:
        deviation = (
            point[series] - point[plan]
            if point is not None and point[series] is not None and point[plan] is not None
            else 0
        )
        sign = (1 if deviation > 0 else -1) if abs(deviation) >= threshold else 0
        if group and (sign != direction or point["minute"] - group[-1][0]["minute"] > max_gap):
            first, last = group[0][0], group[-1][0]
            if last["minute"] - first["minute"] >= duration - 1e-8:
                peak, peak_deviation = max(group, key=lambda item: abs(item[1]))
                events.append(
                    {
                        "series": label,
                        "start": first["minute"],
                        "end": last["minute"],
                        "peak": peak["minute"],
                        "deviation": peak_deviation,
                        "temperature": peak[series],
                        "setpoint": peak[plan],
                        "startAt": first["timestamp"],
                        "endAt": last["timestamp"],
                    }
                )
            group = []
        if sign:
            group.append((point, deviation))
        direction = sign
    return events


def _rolling_predictions(rows, times, artifact, config, tolerance):
    forecasts = {}
    for i, origin in enumerate(times):
        target = origin + config.horizonMinutes
        j = bisect_left(times, target, lo=i + 1)
        if j == len(rows):
            break
        if times[j] - target > tolerance:
            continue
        start = bisect_right(times, origin - config.historyMinutes, hi=i + 1) - 1
        if start < 0:
            continue
        features = make_features(
            rows[start : i + 1],
            rows[j].get("temp_sp"),
            config,
            include_sequence=artifact["algorithm"] == "lstm",
        )
        if features is not None:
            forecasts[j] = predict_model(artifact, features)
    return forecasts


def preview_temperature_record(points, config: ExperimentRequest, algorithm):
    """Describe compatible prediction starts using the same time rules as inference."""
    count = len(points)
    key = target_key(config)
    measured = sum(point.get(key) is not None for point in points)
    planned = sum(point.get("temp_sp") is not None for point in points)
    issues = []
    recommended = None
    minimum_window = None
    if count < 2:
        issues.append("有效时间点不足两个，请检查时间列。")
    elif not measured:
        zone = f"{config.zone} 区实测温度" if config.zone else "实测温度或分区实测温度"
        issues.append(f"当前模型需要{zone}，请检查字段映射。")
    else:
        times = [point["time"] for point in points]
        length = times[-1] - times[0]
        cadence = median(b - a for a, b in zip(times, times[1:], strict=False))
        preferred = times[0] + length * 0.6
        starts = []
        if algorithm == "physics":
            starts = [
                i
                for i, (point, following) in enumerate(zip(points, points[1:], strict=False))
                if point.get(key) is not None
                and point.get("temp_sp") is not None
                and following.get("temp_sp") is not None
                and times[i + 1] - times[i] <= min(cadence * 1.5 + 1e-8, 360)
            ]
            if not starts:
                issues.append("没有同时具备起点实测温度和连续后续设定计划的时段。")
        else:
            tolerance = min(cadence * 0.51, 5)
            for i, point in enumerate(points):
                target = times[i] + config.horizonMinutes
                j = bisect_left(times, target, lo=i + 1)
                if j == count:
                    break
                if (
                    times[i] - times[0] >= config.historyMinutes
                    and point.get(key) is not None
                    and times[j] - target <= tolerance
                    and (
                        algorithm != "fusion"
                        or (
                            point.get("temp_sp") is not None
                            and points[j].get("temp_sp") is not None
                        )
                    )
                ):
                    starts.append(i)
            if not starts:
                issues.append("没有满足该算法历史窗口、预测时长和输入要求的时段。")
        if starts:
            best = min(starts, key=lambda i: abs(times[i] - preferred))
            interval = times[best + 1] - times[best]
            recommended = (times[best] - times[0] + interval * 0.1) / length
            if algorithm == "physics":
                minimum_window = ceil(interval - 1e-8)
    return {
        "count": count,
        "from": points[0]["ts"] if points else None,
        "to": points[-1]["ts"] if points else None,
        "measuredCount": measured,
        "setpointCount": planned,
        "recommendedCursorFraction": recommended,
        "issues": issues,
        "minimumWindowMinutes": minimum_window,
    }


def run_temperature_forecast(cycle, artifact, request: TemperatureForecastRequest, zone_count=0):
    config = ExperimentRequest.model_validate(artifact["config"])
    if config.task != "temperature":
        raise ValueError("所选模型版本缺少温度子模型")
    rows = clean_rows(
        cycle["points"],
        zone_count,
        exclude_zero_temperature=config.excludeZeroTemperatureRecords,
    )
    if len(rows) < 2:
        raise ValueError("该工况记录不足两个采样点")
    times = [row["time"] for row in rows]
    cadence = median(b - a for a, b in zip(times, times[1:], strict=False))
    max_gap = cadence * 1.5 + 1e-8
    start = bisect_right(times, times[0] + (times[-1] - times[0]) * request.cursorFraction) - 1
    origin = rows[start]
    key = target_key(config)
    target_label = f"{config.zone} 区" if config.zone else "有效温区平均"
    dynamic = artifact["algorithm"] == "physics"
    adjusted = bool(request.offsetC or request.tauMinutes is not None)
    if adjusted and not dynamic:
        raise ValueError("当前算法只支持固定时长预测。参数试算需要已保存的温度机理基线")
    stop = bisect_right(times, times[start] + request.durationMinutes + 1e-8)
    points = []
    for i, row in enumerate(rows):
        historical = i <= start
        points.append(
            {
                "minute": times[i] - times[0],
                "timestamp": row["ts"],
                "actual": row.get(key) if historical else None,
                "actualAfter": row.get(key) if not historical else None,
                "setpoint": row.get("temp_sp"),
                "adjustedSetpoint": None,
                "forecast": None,
                "scenario": None,
                "rolling": None,
                "gas": row.get("gas") if historical else None,
                "air": row.get("air") if historical else None,
                "pressure": row.get("pressure") if historical else None,
                "oxygen": row.get("o2") if historical else None,
                "zoneTemperatures": [row.get(f"z{zone}_temp") for zone in range(1, zone_count + 1)]
                if historical
                else [],
            }
        )
    notes = []
    tau = None
    if dynamic:
        if stop <= start + 1:
            raise ValueError("预测起点后没有落在所选窗口内的采样点，请提前起点或增大窗口")
        baseline = scenario = origin.get(key)
        previous_plan = origin.get("temp_sp")
        if baseline is None or previous_plan is None:
            raise ValueError("预测起点缺少实测温度或设定温度，请移动预测起点")
        saved_tau = artifact["parameters"]["tauMinutes"]
        tau = request.tauMinutes if request.tauMinutes is not None else saved_tau
        points[start]["forecast"] = baseline
        if adjusted:
            points[start].update(scenario=scenario, adjustedSetpoint=previous_plan)
        for i in range(start + 1, stop):
            minutes = times[i] - times[i - 1]
            plan = rows[i].get("temp_sp")
            if plan is None or minutes > max_gap:
                notes.append(f"设定缺测或采样中断，预测计算至 {rows[i - 1]['ts']}。")
                break
            baseline = _advance(baseline, previous_plan, plan, minutes, saved_tau)
            points[i]["forecast"] = baseline
            if adjusted:
                scenario, next_plan = _advance_scenario(
                    scenario,
                    previous_plan,
                    plan,
                    times[i - 1] - times[start],
                    minutes,
                    tau,
                    request,
                )
                points[i].update(scenario=scenario, adjustedSetpoint=next_plan)
            previous_plan = plan
        if not any(point["forecast"] is not None for point in points[start + 1 : stop]):
            raise ValueError("起点后缺少连续设定计划，无法计算温度走势")
        if stop == len(rows) and times[-1] - times[start] < request.durationMinutes:
            notes.append("档案剩余计划短于所选窗口，曲线截止到档案末尾。")
        if request.offsetC and times[stop - 1] - times[start] <= request.delayMinutes:
            notes.append("偏移延时超出本次实际计算窗口，设定调整尚未生效。")
        alerts = _alerts(
            points[start + 1 : stop],
            "forecast",
            "setpoint",
            request.deviationC,
            request.deviationMinutes,
            max_gap,
            "原计划",
        )
        if adjusted:
            alerts += _alerts(
                points[start + 1 : stop],
                "scenario",
                "adjustedSetpoint",
                request.deviationC,
                request.deviationMinutes,
                max_gap,
                "试算计划",
            )
        scope = "起点实测温度作为初值，按未来已知设定计划逐采样段求解一阶响应。"
    else:
        rolling = _rolling_predictions(rows, times, artifact, config, min(cadence * 0.51, 5))
        for index, predicted in rolling.items():
            points[index]["rolling"] = predicted
        j = bisect_left(times, times[start] + config.horizonMinutes, lo=start + 1)
        if j == len(rows) or times[j] - times[start] - config.horizonMinutes > min(
            cadence * 0.51, 5
        ):
            notes.append("当前起点无法对齐已训练的预测时长，可移动起点查看对应预测点。")
        else:
            features = make_features(
                rows[: start + 1],
                rows[j].get("temp_sp"),
                config,
                include_sequence=artifact["algorithm"] == "lstm",
            )
            if features is not None:
                points[j]["forecast"] = predict_model(artifact, features)
        if not rolling:
            raise ValueError("该记录没有满足模型历史窗口与预测时长的样本")
        alerts = []
        scope = (
            f"该模型训练于固定的 {config.horizonMinutes} 分钟时长。"
            "滚动回放的每个预测点使用各自起点已知的历史，选中起点只输出该时长的预测点。"
        )
    future = [p for p in points[start + 1 :] if p["forecast"] is not None]
    return {
        "mode": "trajectory" if dynamic else "rolling",
        "cycleId": cycle["id"],
        "cycleName": cycle["name"],
        "zone": config.zone,
        "targetLabel": target_label,
        "algorithm": artifact["algorithm"],
        "originAt": origin["ts"],
        "originMinute": points[start]["minute"],
        "scope": scope,
        "request": request.model_dump(),
        "points": points,
        "alerts": alerts,
        "notes": notes,
        "metrics": {
            "pointCount": len(future),
            "endAt": future[-1]["timestamp"] if future else None,
            "endTemperature": future[-1]["forecast"] if future else None,
            "scenarioEndTemperature": future[-1]["scenario"] if future else None,
            "peakTemperature": max((p["forecast"] for p in future), default=None),
            "savedTauMinutes": artifact["parameters"]["tauMinutes"] if dynamic else None,
            "scenarioTauMinutes": tau,
            "trainedHorizonMinutes": config.horizonMinutes,
        },
    }
