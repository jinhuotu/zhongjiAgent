"""手动选型、保留原始数据的未来计划试算和可追溯提醒。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.kiln_predict import (
    CO2_KG_PER_NM3,
    ZONE_NAMES,
    _physics_at,
    _slopes,
    _stage,
)

ModelKey = Literal["similar", "data", "physics", "fusion"]


class ScenarioParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    temperatureOffsetC: float = Field(default=0, ge=-100, le=100)
    remainingDurationScale: float = Field(default=1, ge=0.5, le=1.5)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    selectedModel: ModelKey
    cursorHour: float = Field(ge=0)
    parameters: ScenarioParameters = Field(default_factory=ScenarioParameters)
    instruction: str = Field(default="", max_length=1500)


def integrate_points(points: list[dict[str, Any]], key: str, start: float, end: float) -> float:
    """按实际区间端点插值后积分，缺测区间不当作零消耗。"""
    total = 0.0
    for left, right in zip(points, points[1:], strict=False):
        width = right["hour"] - left["hour"]
        a, b = max(start, left["hour"]), min(end, right["hour"])
        lv, rv = left.get(key), right.get(key)
        if width <= 0 or b <= a or lv is None or rv is None:
            continue
        va = lv + (rv - lv) * (a - left["hour"]) / width
        vb = lv + (rv - lv) * (b - left["hour"]) / width
        total += (va + vb) * (b - a) / 2
    return total


def _at(points: list[dict[str, Any]], hour: float) -> dict[str, Any]:
    if hour <= points[0]["hour"]:
        return deepcopy(points[0])
    for left, right in zip(points, points[1:], strict=False):
        if hour > right["hour"]:
            continue
        if hour == right["hour"]:
            return deepcopy(right)
        fraction = (hour - left["hour"]) / (right["hour"] - left["hour"])
        row = deepcopy(left)
        row["hour"] = hour
        for key in (
            "tempSp",
            "tempMeas",
            "measured",
            "similar",
            "data",
            "physics",
            "fusion",
        ):
            a, b = left.get(key), right.get(key)
            row[key] = a + (b - a) * fraction if a is not None and b is not None else None
        return row
    return deepcopy(points[-1])


def _temperature_forecast(
    points: list[dict[str, Any]], cursor: float
) -> dict[float, tuple[float | None, float | None, float | None]]:
    """根据当前实测温度和未来设定曲线计算一阶温度响应预测。

    这是可解释的基线模型，响应速度按工艺阶段变化，区间表示模型估计不确定性。
    """
    current = _at(points, cursor)
    previous = current.get("tempMeas")
    if previous is None:
        previous = current.get("tempSp")
    if previous is None:
        return {}
    result: dict[float, tuple[float | None, float | None, float | None]] = {}
    previous_hour = cursor
    for row in points:
        hour = float(row["hour"])
        if hour <= cursor:
            continue
        target = row.get("tempSp")
        if target is None:
            result[hour] = (None, None, None)
            continue
        delta_hours = max(0.001, hour - previous_hour)
        stage = row.get("stage") or "升温"
        gain = {"预热": 0.30, "升温": 0.24, "保温": 0.16, "降温": 0.28}.get(stage, 0.22)
        response = min(0.85, gain * max(1.0, delta_hours / 0.1))
        estimate = previous + (target - previous) * response
        max_change = 180.0 * delta_hours
        estimate = previous + max(-max_change, min(max_change, estimate - previous))
        horizon = max(0.0, hour - cursor)
        uncertainty = 18.0 + 8.0 * horizon**0.5 + 0.05 * abs(target - estimate)
        result[hour] = (
            round(estimate, 2),
            round(max(0.0, estimate - uncertainty), 2),
            round(estimate + uncertainty, 2),
        )
        previous = estimate
        previous_hour = hour
    return result


def _zone_temperature_forecasts(
    points: list[dict[str, Any]], cursor: float
) -> dict[float, dict[str, tuple[float | None, float | None, float | None]]]:
    forecasts: dict[float, dict[str, tuple[float | None, float | None, float | None]]] = {}
    for zone_name in ZONE_NAMES:
        synthetic: list[dict[str, Any]] = []
        for point in points:
            row = dict(point)
            reading = next(
                (item for item in point.get("zones", []) if item.get("name") == zone_name),
                {},
            )
            row["tempMeas"] = reading.get("temp")
            synthetic.append(row)
        for hour, forecast in _temperature_forecast(synthetic, cursor).items():
            forecasts.setdefault(hour, {})[zone_name] = forecast
    return forecasts


def build_analysis(workspace: dict[str, Any], request: AnalysisRequest) -> dict[str, Any]:
    points = deepcopy(workspace["points"])
    if len(points) < 2:
        raise ValueError("至少需要两个有效时间点才能进行预测试算")
    cursor, duration = request.cursorHour, workspace["durationHours"]
    if cursor > duration:
        raise ValueError("当前时刻不能超过窑次结束时间")
    selected = next(m for m in workspace["models"] if m["key"] == request.selectedModel)
    if not selected["available"]:
        raise ValueError(f"{selected['name']}不可用：{selected['note']}")
    future = [p for p in points if p["hour"] >= cursor]
    if any(p.get(request.selectedModel) is None for p in future):
        raise ValueError("所选模型未覆盖全部剩余计划，请补齐温度数据或选择其他模型")
    params = request.parameters
    changed = params.temperatureOffsetC != 0 or params.remainingDurationScale != 1
    if changed and cursor >= duration:
        raise ValueError("窑次已结束，没有可调整的剩余计划")
    if request.selectedModel == "similar" and params.temperatureOffsetC != 0:
        raise ValueError("相似窑次基线支持时长对齐，暂不支持温度偏移试算")
    if (
        changed
        and request.selectedModel != "similar"
        and any(p.get("tempSp") is None for p in future)
    ):
        raise ValueError("未来设定温度计划不完整，不能进行工艺参数试算")

    current = _at(points, cursor)
    if current.get(request.selectedModel) is None and cursor < duration:
        raise ValueError("当前时刻附近缺少模型输入，请调整回放时刻或补齐数据")
    if not any(abs(p["hour"] - cursor) < 1e-8 for p in points):
        points.append(current)
        points.sort(key=lambda p: p["hour"])
    base_total = integrate_points(points, request.selectedModel, cursor, duration)
    used = integrate_points(points, "measured", 0, cursor)
    end = cursor + (duration - cursor) * params.remainingDurationScale
    start_ts = workspace.get("cycle", {}).get("startTs")
    origin = datetime.fromisoformat(start_ts) if start_ts else None
    adjusted = []
    for row in points:
        hour = row["hour"]
        is_future = hour > cursor
        new_hour = cursor + (hour - cursor) * params.remainingDurationScale if is_future else hour
        temp = row.get("tempSp")
        if is_future and temp is not None:
            # 从当前时刻连续过渡，15 分钟后达到用户指定偏移。
            temp += params.temperatureOffsetC * min((new_hour - cursor) / 0.25, 1)
        if temp is not None and not 0 <= temp <= 1800:
            raise ValueError("调整后的设定温度须在 0–1800 ℃ 范围内")
        zones = deepcopy(row.get("zones") or [])
        if is_future:
            for zone in zones:
                zone["temp"] = None
        adjusted.append(
            {
                "hour": new_hour,
                "sourceHour": hour,
                "timestamp": (origin + timedelta(hours=new_hour)).isoformat() if origin else None,
                "stage": row["stage"],
                "tempSp": temp,
                "baselineTempSp": row.get("tempSp"),
                "tempMeas": row.get("tempMeas") if not is_future else None,
                "measured": row.get("measured") if not is_future else None,
                "baseline": row.get(request.selectedModel),
                "predicted": row.get(request.selectedModel),
                "zones": zones,
            }
        )

    baseline_temperature = _temperature_forecast(points, cursor)
    adjusted_temperature = _temperature_forecast(adjusted, cursor)
    baseline_zone_temperature = _zone_temperature_forecasts(points, cursor)
    adjusted_zone_temperature = _zone_temperature_forecasts(adjusted, cursor)
    for row in adjusted:
        forecast = adjusted_temperature.get(float(row["hour"]))
        row["predictedTemperature"] = forecast[0] if forecast else None
        row["temperatureLow"] = forecast[1] if forecast else None
        row["temperatureHigh"] = forecast[2] if forecast else None
        row["zonePredictedTemperature"] = {
            name: values[0]
            for name, values in adjusted_zone_temperature.get(float(row["hour"]), {}).items()
        }
    slopes = _slopes([p["hour"] for p in adjusted], [p["tempSp"] for p in adjusted])
    maximum = max((p["tempSp"] for p in adjusted if p["tempSp"] is not None), default=0)
    report = workspace["report"]
    for index, row in enumerate(adjusted):
        if row["hour"] <= cursor or not changed or request.selectedModel == "similar":
            continue
        row["stage"] = _stage(row["tempSp"], slopes[index], maximum)
        value = _physics_at(slopes[index], row["tempSp"], report["alpha"], report["beta"])
        if request.selectedModel == "data":
            a, b, c = workspace["dataCoefficients"]
            value = min(300, max(0, a + b * row["tempSp"] + c * max(0, slopes[index])))
        elif request.selectedModel == "fusion" and value is not None:
            value = max(0, value + report["residuals"].get(row["stage"], 0))
        row["predicted"] = value

    remaining = integrate_points(adjusted, "predicted", cursor, end)
    warnings = [*workspace["gaps"], *workspace["notes"]]
    if any(p["measured"] is None for p in points if p["hour"] <= cursor):
        warnings.append("已发生燃气存在缺测，累计用气仅覆盖有效区间，整窑估计可能偏低")
    warnings.append("装载量仅参与相似窑次匹配；当前模型不支持装载变化的热负荷试算")
    if changed:
        warnings.append("调整结果为模型情景估计，未验证产品质量或设备执行能力，不自动下发控制指令")

    alerts: list[dict[str, Any]] = []

    def alert(
        key: str,
        level: str,
        title: str,
        detail: str,
        evidence: str,
        zone: str | None = None,
        at: float = cursor,
    ) -> None:
        alerts.append(
            {
                "id": key,
                "level": level,
                "title": title,
                "detail": detail,
                "evidence": evidence,
                "zone": zone,
                "startHour": round(at, 3),
                "endHour": round(min(end, at + 0.5), 3),
                "modelKey": request.selectedModel,
            }
        )

    alert(
        "remaining",
        "info",
        "剩余用气预测",
        f"预计剩余 {remaining:.1f} Nm³",
        f"采用用户选择的{selected['name']}，对当前时刻之后的预测流量积分。",
    )
    for index, zone in enumerate(current.get("zones") or []):
        if zone.get("temp") is None or current.get("tempSp") is None:
            continue
        deviation = zone["temp"] - current["tempSp"]
        if abs(deviation) >= 30:
            alert(
                f"zone-{index}",
                "high" if abs(deviation) >= 60 else "medium",
                f"{zone['name']}温度偏差",
                f"实测与设定相差 {deviation:+.1f} ℃",
                "按上传数据对照当前设定温度，提醒阈值为绝对偏差 30 ℃；属于当前状态提醒。",
                zone["name"],
            )
    oxygen, target_o2 = current.get("o2"), current.get("o2Sp")
    if oxygen is not None and target_o2 is not None and abs(oxygen - target_o2) >= 2:
        alert(
            "oxygen",
            "medium",
            "氧含量偏离设定",
            f"实测 {oxygen:.2f}%，设定 {target_o2:.2f}%",
            "实测与文件中氧含量设定相差至少 2 个百分点；属于当前状态提醒。",
        )
    if cursor < end:
        peak = max((p for p in adjusted if p["hour"] > cursor), key=lambda p: p["predicted"] or 0)
        alert(
            "peak",
            "info",
            "未来计划用气峰值",
            f"预计峰值 {peak['predicted']:.1f} Nm³/h",
            "在所选模型的剩余计划预测曲线上寻找最大值；这是用气预测，并非炉温预测。",
            at=peak["hour"],
        )
    future_temperature = [
        row
        for row in adjusted
        if row["hour"] > cursor and row.get("predictedTemperature") is not None
    ]
    if future_temperature:
        largest_gap = max(
            future_temperature,
            key=lambda row: abs((row.get("tempSp") or 0) - row["predictedTemperature"]),
        )
        gap = largest_gap["predictedTemperature"] - (largest_gap.get("tempSp") or 0)
        if abs(gap) >= 30:
            alert(
                "temperature-forecast-gap",
                "high" if abs(gap) >= 60 else "medium",
                "预计温度偏离设定曲线",
                f"约 {largest_gap['hour']:.2f} h 时预测 "
                f"{largest_gap['predictedTemperature']:.1f} ℃，"
                f"比设定 {'高' if gap > 0 else '低'} {abs(gap):.1f} ℃",
                "温度预测基于当前实测温度、未来设定曲线和阶段响应模型，预测区间随时间扩大。",
                at=largest_gap["hour"],
            )
    windows = []
    for minutes in (15, 30, 60):
        stop = min(end, cursor + minutes / 60)
        windows.append(
            {
                "minutes": minutes,
                "coveredMinutes": round((stop - cursor) * 60, 1),
                "gas": round(integrate_points(adjusted, "predicted", cursor, stop), 2),
            }
        )
    return {
        "selectedModel": selected,
        "cursorHour": cursor,
        "durationHours": end,
        "parameters": params.model_dump(),
        "adjusted": changed,
        "points": adjusted,
        "baselinePoints": [
            {
                "hour": p["hour"],
                "predicted": p.get(request.selectedModel),
                "tempSp": p.get("tempSp"),
                "predictedTemperature": baseline_temperature.get(
                    float(p["hour"]), (None, None, None)
                )[0],
                "temperatureLow": baseline_temperature.get(float(p["hour"]), (None, None, None))[1],
                "temperatureHigh": baseline_temperature.get(float(p["hour"]), (None, None, None))[
                    2
                ],
                "zonePredictedTemperature": {
                    name: values[0]
                    for name, values in baseline_zone_temperature.get(float(p["hour"]), {}).items()
                },
            }
            for p in points
        ],
        "current": current,
        "alerts": alerts,
        "windows": windows,
        "metrics": {
            "usedGas": round(used, 2),
            "baselineRemainingGas": round(base_total, 2),
            "remainingGas": round(remaining, 2),
            "wholeGas": round(used + remaining, 2),
            "baselineWholeGas": round(used + base_total, 2),
            "deltaGas": round(remaining - base_total, 2),
            "carbonKg": round((used + remaining) * CO2_KG_PER_NM3, 2),
        },
        "warnings": list(dict.fromkeys(warnings)),
        "temperatureModel": {
            "name": "阶段响应温度预测 V1",
            "target": "六区平均温度",
            "horizon": "当前时刻之后的计划曲线",
            "description": "当前实测温度向未来设定曲线的一阶响应预测，区间随预测时长扩大。",
        },
        "scope": "历史数据回放及未来计划试算；15/30/60 分钟为预测窗口，尚未进行在线独立精度验证",
    }
