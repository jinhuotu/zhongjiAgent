"""回放已保存的预测模型，预测输入与事后实测分离。"""

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from math import isfinite
from statistics import median
from typing import Any

from common.kiln_studio_engine import make_features, physics, predict_model, target_key
from common.kiln_studio_forecast import _advance, _advance_scenario
from common.kiln_studio_lstm import predict_lstm_path
from common.kiln_studio_observations import clean_rows as _clean_rows
from common.kiln_studio_schema import ExperimentRequest, ReplayFrame, ReplayPackage, ReplayRequest

_SIGNALS = ("temp", "temp_sp", "gas", "air", "afr", "o2", "pressure")
_HELD_SIGNALS = ("temp", "gas", "air", "o2", "pressure")
_MAX_REPLAY_POINTS = 300_000
_EPSILON = 1e-7


def _snapshot(row, zone_count):
    return {
        **{key: row[key] for key in _SIGNALS},
        "zones": [
            {
                "zone": zone,
                "temp": row[f"z{zone}_temp"],
                "gas": row[f"z{zone}_gas"],
                "air": row[f"z{zone}_air"],
                "airValve": row[f"z{zone}_air_valve"],
            }
            for zone in range(1, zone_count + 1)
        ],
    }


def _plan_at(times, plans, minute, max_gap):
    right = bisect_left(times, minute - _EPSILON)
    if right == len(times):
        return None
    if abs(times[right] - minute) <= _EPSILON:
        return plans[right]
    if right == 0 or times[right] - times[right - 1] > max_gap:
        return None
    start, finish = plans[right - 1], plans[right]
    if start is None or finish is None:
        return None
    fraction = (minute - times[right - 1]) / (times[right] - times[right - 1])
    return start + (finish - start) * fraction


def _mechanism_points(temp, start, length, times, plans, max_gap, tau, *, scenario=None):
    end = start + length
    first = bisect_right(times, start + _EPSILON)
    last = bisect_left(times, end - _EPSILON)
    # 同时保留设定计划折点和显示用的整数分钟，按原计划的分段斜率求解。
    offsets = sorted(
        set(range(1, length + 1))
        | {t - start for t in times[first:last] if abs(t - start - round(t - start)) > _EPSILON}
    )
    previous, plan = 0.0, _plan_at(times, plans, start, max_gap)
    values = {}
    for offset in offsets:
        next_plan = _plan_at(times, plans, start + offset, max_gap)
        midpoint = start + (previous + offset) / 2
        if plan is None or next_plan is None or _plan_at(times, plans, midpoint, max_gap) is None:
            break
        if offset - previous <= _EPSILON:
            continue
        if scenario is None:
            temp = _advance(temp, plan, next_plan, offset - previous, tau)
        else:
            temp, _ = _advance_scenario(
                temp, plan, next_plan, previous, offset - previous, tau, scenario
            )
        previous, plan = offset, next_plan
        if offset in range(1, length + 1):
            values[int(offset)] = temp
    return values


def _series(length, values):
    return [
        {"minuteOffset": minute, "value": values.get(minute)} for minute in range(1, length + 1)
    ]


def _independent_mechanism(origin, config, parameters, length, times, plans, max_gap):
    if parameters is None:
        return None, "本次实验的机理版本未启用当前任务对应的求解机理。"
    key, start = target_key(config), origin["time"]
    if origin[key] is None:
        detail = (
            "起点流量缺测或燃气低于空燃比计算阈值。"
            if config.task == "afr"
            else "预测起点缺少实测值。"
        )
        return None, detail
    available = min(length, int(times[-1] - start + _EPSILON))
    if config.task == "temperature":
        values = _mechanism_points(
            origin[key], start, available, times, plans, max_gap, parameters["tauMinutes"]
        )
    elif config.task == "afr":
        reference = physics({}, parameters, "afr", 1)
        values = dict.fromkeys(range(1, available + 1), reference)
    else:
        values = {}
        for offset in range(1, available + 1):
            left, right = start + offset - 1, start + offset
            before, after = (
                _plan_at(times, plans, left, max_gap),
                _plan_at(times, plans, right, max_gap),
            )
            if (
                before is None
                or after is None
                or _plan_at(times, plans, (left + right) / 2, max_gap) is None
            ):
                break
            values[offset] = physics(
                {"sp0": before, "sp1": after, "slope": (after - before) * 60},
                parameters,
                "energy",
                1,
            )
    issue = None
    if len(values) < available:
        issue = "设定计划缺失或断档，机理曲线显示至最后可计算时刻。"
    return _series(length, values), issue


def temperature_trajectory_method(artifact, config):
    if config.task != "temperature":
        return None
    key = artifact.get("implementationKey", artifact["algorithm"])
    if key == "physics":
        return "mechanism"
    if key == "baseline":
        return "persistence"
    if config.horizonMinutes == 1:
        return "minuteRecursive"
    if key == "lstm" and set(range(1, config.horizonMinutes + 1)).issubset(
        artifact["model"].get("offsets") or ()
    ):
        return "multiOutput"
    return "fixedHorizon"


def _continuous_end(times, start, end, max_gap):
    first = bisect_right(times, start + _EPSILON)
    stop = min(len(times), bisect_left(times, end - _EPSILON, lo=first) + 1)
    for index in range(first, stop):
        if times[index] - times[index - 1] > max_gap:
            return times[index - 1]
    return min(end, times[-1])


def _recursive_points(history, held, artifact, config, key, length, times, plans, max_gap):
    origin = history[-1]
    start = origin["time"]
    method = temperature_trajectory_method(artifact, config)
    if method == "persistence":
        end = _continuous_end(times, start, start + length, max_gap)
        available = min(length, int(end - start + _EPSILON))
        value = predict_model(artifact, {"y0": origin[key]})
        return _series(length, dict.fromkeys(range(1, available + 1), value)), None
    date = datetime.fromisoformat(origin["ts"])
    native_step = config.horizonMinutes
    fusion = config.task == "temperature" and artifact["algorithm"] == "fusion"
    predictions, reference = {}, {}
    virtual = list(history)
    lstm = artifact["algorithm"] == "lstm"
    for segment_start in range(0, length, native_step):
        offset = segment_start + native_step
        if offset > length and not lstm:
            break
        minute = start + offset
        if minute > times[-1] + _EPSILON:
            break
        if (lstm or method is not None) and _continuous_end(
            times, start + segment_start, minute, max_gap
        ) < minute - _EPSILON:
            break
        setpoint = _plan_at(times, plans, minute, max_gap)
        features = make_features(virtual, setpoint, config, include_sequence=lstm)
        if features is None:
            break
        path = predict_lstm_path(artifact, features) if lstm else {}
        value = path.get(native_step) if lstm else predict_model(artifact, features)
        if value is None or not isfinite(value):
            break
        if lstm and config.task in {"energy", "afr"}:
            value = max(0, value)
        if fusion:
            params = artifact["parameters"]
            base = _mechanism_points(
                virtual[-1][key],
                start + segment_start,
                native_step,
                times,
                plans,
                max_gap,
                params["tauMinutes"],
            )
            reference.update({segment_start + index: point for index, point in base.items()})
            if native_step not in base:
                break
            anchor_base = physics(features, params, config.task, native_step)
            if anchor_base is None:
                break
            # 残差只在训练时距节点应用一次。节点之间展示机理参考。
            value = base[native_step] + (value - anchor_base)
        if not isfinite(value):
            break
        if lstm:
            for step, intermediate in path.items():
                elapsed = segment_start + step
                if step >= native_step or elapsed > length or not isfinite(intermediate):
                    continue
                intermediate = (
                    max(0, intermediate) if config.task in {"energy", "afr"} else intermediate
                )
                predictions[elapsed] = intermediate
                virtual.append(
                    {
                        **held,
                        key: intermediate,
                        "time": start + elapsed,
                        "ts": (date + timedelta(minutes=elapsed)).isoformat(),
                        "cycle": origin["cycle"],
                        "temp_sp": _plan_at(times, plans, start + elapsed, max_gap),
                    }
                )
        if offset > length:
            break
        predictions[offset] = value
        # 起点之后仅回填预测目标，其他观测量保持起点前最后一次有效读数。
        virtual.append(
            {
                **held,
                key: value,
                "time": minute,
                "ts": (date + timedelta(minutes=offset)).isoformat(),
                "cycle": origin["cycle"],
                "temp_sp": setpoint,
            }
        )
    if fusion:
        previous_offset = max(predictions, default=0)
        remaining = min(length - previous_offset, native_step)
        if remaining:
            tail = _mechanism_points(
                virtual[-1][key],
                start + previous_offset,
                remaining,
                times,
                plans,
                max_gap,
                artifact["parameters"]["tauMinutes"],
            )
            reference.update({previous_offset + index: point for index, point in tail.items()})
    return _series(length, predictions), _series(length, reference) if fusion else None


def _frame_indices(times, step, origin_after):
    indices = []
    next_at = times[0]
    for index, minute in enumerate(times[:-1]):
        if origin_after is not None and minute <= origin_after + _EPSILON:
            continue
        if minute < next_at - _EPSILON:
            continue
        indices.append(index)
        next_at = minute + step
    return indices


def build_replay_package(
    cycles,
    artifacts,
    request: ReplayRequest,
    zone_count,
    *,
    origin_after: float | None = None,
    mechanism_parameters=None,
) -> ReplayPackage:
    cycle = next((item for item in cycles if item["id"] == request.cycleId), None)
    if cycle is None or len(cycle["points"]) < 2:
        raise ValueError("窑次不属于该数据版本，或有效时间点不足")
    selected: list[tuple[dict[str, Any], ExperimentRequest]] = []
    for name in request.selectedAlgorithms:
        artifact = artifacts.get(name)
        if artifact is None or artifact.get("algorithm") != name:
            raise ValueError(f"实验缺少算法训练产物：{name}")
        config = ExperimentRequest.model_validate(artifact["config"])
        if selected and config != selected[0][1]:
            raise ValueError("算法产物的实验配置不一致")
        selected.append((artifact, config))
    config = selected[0][1]
    if not 0 <= config.zone <= zone_count:
        raise ValueError("实验预测温区超出数据版本范围")
    if config.task != "temperature" and config.zone:
        raise ValueError("分区选择仅用于温度实验")
    afr_floor = None
    if config.task == "afr":
        afr_floor = selected[0][0]["parameters"]["minGas"]
        if (
            not isfinite(afr_floor)
            or afr_floor <= 0
            or any(artifact["parameters"]["minGas"] != afr_floor for artifact, _ in selected)
        ):
            raise ValueError("实验算法的有效燃气阈值不一致或无效")
    key = target_key(config)
    rows = _clean_rows(
        cycle["points"],
        zone_count,
        afr_floor,
        exclude_zero_temperature=config.excludeZeroTemperatureRecords,
    )
    if config.task == "energy":
        for row in rows:
            if row["gas"] is not None and row["gas"] < 0:
                row.update(gas=None, afr=None)
    times = [row["time"] for row in rows]
    gaps = [right - left for left, right in zip(times, times[1:], strict=False)]
    if any(gap <= 0 or not isfinite(gap) for gap in gaps):
        raise ValueError("窑次时间戳必须有效且严格递增")
    max_gap = median(gaps) * 1.5 + _EPSILON
    plans = [row["temp_sp"] for row in rows]
    limit = request.horizonMinutes
    indices = _frame_indices(times, request.frameStepMinutes, origin_after)
    excluded_first = excluded_stop = len(rows)
    if request.excludedRange is not None:
        exclude_start, exclude_end = request.excludedRange.bounds()
        excluded_first = bisect_left(times, exclude_start)
        excluded_stop = bisect_left(times, exclude_end)
        if excluded_first == excluded_stop:
            raise ValueError("排除区间内没有该窑次的历史记录")
        indices = [index for index in indices if not excluded_first <= index < excluded_stop]
        if (
            excluded_stop < len(rows)
            and cycle["points"][excluded_stop].get("gas_total") is not None
        ):
            # 旧归档无法区分直接流量和用前一条累计量推导的流量。
            rows[excluded_stop].update(gas=None, afr=None)
    if not indices:
        raise ValueError("所选窑次未剩下训练结束后的可回放时段")
    stops = [bisect_right(times, times[index] + limit + _EPSILON) for index in indices]
    series_count = (
        len(selected)
        + (config.task == "temperature" and "fusion" in request.selectedAlgorithms)
        + (request.includeMechanism and mechanism_parameters is not None)
    )
    actual_count = sum(stop - index - 1 for index, stop in zip(indices, stops, strict=True))
    if len(indices) * limit * series_count + actual_count > _MAX_REPLAY_POINTS:
        raise ValueError("回放包太大，请增大帧步长或减少所选算法")
    frames: list[ReplayFrame] = []
    held: dict[str, float | None] = dict.fromkeys(_HELD_SIGNALS)
    seen = 0
    times_before_gap = times[:excluded_first] if request.excludedRange else times
    for index, stop in zip(indices, stops, strict=True):
        after_exclusion = excluded_stop <= index
        if after_exclusion and seen < excluded_stop:
            held = dict.fromkeys(_HELD_SIGNALS)
            seen = excluded_stop
        for row in rows[seen : index + 1]:
            held.update(
                {signal: row[signal] for signal in _HELD_SIGNALS if row[signal] is not None}
            )
        seen = index + 1
        origin = rows[index]
        start = times[index]
        first = max(0, bisect_right(times, start - config.historyMinutes, hi=index + 1) - 1)
        if after_exclusion:
            first = max(first, excluded_stop)
        forecast_times = times if after_exclusion or stop <= excluded_first else times_before_gap
        history = rows[first : index + 1]
        history_missing = after_exclusion and start - times[excluded_stop] < config.historyMinutes
        trajectories = {}
        reference = None
        for artifact, model_config in selected:
            name = artifact["algorithm"]
            if origin[key] is None:
                trajectories[name] = _series(limit, {})
            elif name == "physics" and config.task == "temperature":
                trajectories[name] = _series(
                    limit,
                    _mechanism_points(
                        origin[key],
                        start,
                        limit,
                        forecast_times,
                        plans,
                        max_gap,
                        artifact["parameters"]["tauMinutes"],
                    ),
                )
            elif history_missing:
                trajectories[name] = _series(limit, {})
            else:
                trajectories[name], model_reference = _recursive_points(
                    history,
                    held,
                    artifact,
                    model_config,
                    key,
                    limit,
                    forecast_times,
                    plans,
                    max_gap,
                )
                if model_reference is not None:
                    reference = model_reference
        mechanism, mechanism_issue = (None, None)
        if request.includeMechanism:
            mechanism, mechanism_issue = _independent_mechanism(
                origin, config, mechanism_parameters, limit, forecast_times, plans, max_gap
            )
        frames.append(
            ReplayFrame.model_validate(
                {
                    "frameId": len(frames),
                    "originTs": origin["ts"],
                    "originMinute": start - times[0],
                    "snapshot": _snapshot(origin, zone_count),
                    "future": trajectories,
                    "actualFuture": [
                        {
                            "minuteOffset": rows[k]["time"] - start,
                            "value": None if excluded_first <= k < excluded_stop else rows[k][key],
                        }
                        for k in range(index + 1, stop)
                    ],
                    "fusionReference": reference,
                    "mechanismFuture": mechanism,
                    "mechanismIssue": mechanism_issue,
                }
            )
        )
    label, unit = {
        "temperature": (f"{config.zone} 区温度" if config.zone else "有效均温", "℃"),
        "energy": ("燃气流量", "Nm³/h"),
        "afr": ("空燃比", "Nm³/Nm³"),
    }[config.task]
    return ReplayPackage.model_validate(
        {
            "request": request,
            "cycleName": cycle["name"],
            "zone": config.zone,
            "targetLabel": label,
            "task": config.task,
            "unit": unit,
            "algorithms": [
                {
                    "key": model["algorithm"],
                    "name": model["algorithm"],
                    "mode": (
                        {"physics": "mechanism", "fusion": "fusion"}.get(
                            model["algorithm"], "recursive"
                        )
                        if config.task == "temperature"
                        else "recursive"
                    ),
                    "trainedHorizonMinutes": cfg.horizonMinutes,
                    "temperatureTrajectory": temperature_trajectory_method(model, cfg),
                }
                for model, cfg in selected
            ],
            "baseSeries": [
                {
                    "minute": row["time"] - times[0],
                    "ts": row["ts"],
                    "actual": None if excluded_first <= index < excluded_stop else row[key],
                    "setpoint": None if excluded_first <= index < excluded_stop else row["temp_sp"],
                }
                for index, row in enumerate(rows)
            ],
            "frames": frames,
            "excludedCount": excluded_stop - excluded_first,
            "notes": [
                "按档案中保存的设定计划回放。事后实测仅供核对。",
                "数据模型的递归多步会累积误差，预测时长越长，偏差可能越大。",
                *(
                    [f"燃气流量低于 {afr_floor:g} Nm³/h 或助燃风缺测时，空燃比留空。"]
                    if afr_floor is not None
                    else []
                ),
                "建议供参考，实际收益以现场验证为准。",
            ],
        }
    )
