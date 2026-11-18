"""选型一致性、未来计划隔离、输入边界和 AI 组件约束的回归测试。"""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from common.kiln_analysis import AnalysisRequest, build_analysis, integrate_points
from common.kiln_predict import CycleSeries, build_workspace


def sample(cycle_id: int, start: datetime) -> CycleSeries:
    points = []
    for i in range(121):
        hour = i / 6
        temp = 100 + hour * 80 if hour < 10 else 900
        gas = 0.18 * (80 if hour < 10 else 0) + 0.038 * (temp - 40) + 3
        points.append(
            {
                "ts": start + timedelta(hours=hour),
                "temp_sp": temp,
                "zone_temp": temp - 40,
                "gas": gas,
                "air": 100,
                "o2": 7,
                "o2_sp": 4,
                "furnace_p": 3,
                "zones": [{"name": "一区", "temp": temp - 40, "gas": gas, "air": 100, "valve": 40}],
            }
        )
    return CycleSeries(cycle_id, "TC-03", str(cycle_id), f"{cycle_id}.xls", "V1", 800, points)


def workspace():
    start = datetime(2026, 10, 1)
    target = sample(2, start)
    result = build_workspace(target, [sample(1, start - timedelta(days=3))], downsample=False)
    result["cycle"] = {"startTs": start.isoformat(), "label": "TC-03"}
    return result


@pytest.mark.parametrize("model", ["similar", "data", "physics", "fusion"])
def test_manual_selection_drives_forecast_and_alerts(model):
    ws = workspace()
    assert next(m for m in ws["models"] if m["key"] == model)["available"]
    result = build_analysis(ws, AnalysisRequest(selectedModel=model, cursorHour=8.25))
    expected = integrate_points(ws["points"], model, 8.25, 20)
    assert result["selectedModel"]["key"] == model
    assert result["metrics"]["remainingGas"] == pytest.approx(expected, abs=0.02)
    assert all(a["modelKey"] == model for a in result["alerts"])
    assert all(
        p["measured"] is None and p["tempMeas"] is None
        for p in result["points"]
        if p["hour"] > 8.25
    )
    assert result["metrics"]["wholeGas"] == pytest.approx(
        result["metrics"]["usedGas"] + result["metrics"]["remainingGas"], abs=0.02
    )
    assert result["temperatureModel"]["target"] == "六区平均温度"
    future_temperature = [
        point["predictedTemperature"] for point in result["points"] if point["hour"] > 8.25
    ]
    assert future_temperature and all(value is not None for value in future_temperature)
    assert all(
        point["temperatureLow"] <= point["predictedTemperature"] <= point["temperatureHigh"]
        for point in result["points"]
        if point["hour"] > 8.25
    )


def test_scenario_preserves_source_and_past_but_changes_future():
    ws = workspace()
    original = deepcopy(ws)
    baseline = build_analysis(ws, AnalysisRequest(selectedModel="physics", cursorHour=8))
    adjusted = build_analysis(
        ws,
        AnalysisRequest(
            selectedModel="physics",
            cursorHour=8,
            parameters={"temperatureOffsetC": 40, "remainingDurationScale": 1.2},
        ),
    )
    assert ws == original
    assert adjusted["metrics"]["usedGas"] == baseline["metrics"]["usedGas"]
    assert adjusted["durationHours"] == pytest.approx(22.4)
    assert adjusted["metrics"]["remainingGas"] != baseline["metrics"]["remainingGas"]
    assert [p for p in baseline["points"] if p["hour"] <= 8] == [
        p for p in adjusted["points"] if p["hour"] <= 8
    ]
    assert adjusted["points"][-1]["tempSp"] == 940
    assert adjusted["baselinePoints"] == baseline["baselinePoints"]
    assert (
        adjusted["points"][-1]["predictedTemperature"]
        != baseline["points"][-1]["predictedTemperature"]
    )


def test_model_availability_and_scenario_capabilities():
    ws = workspace()
    with pytest.raises(ValueError, match="不支持温度偏移"):
        build_analysis(
            ws,
            AnalysisRequest(
                selectedModel="similar", cursorHour=8, parameters={"temperatureOffsetC": 20}
            ),
        )
    ws["models"][1]["available"] = False
    with pytest.raises(ValueError, match="不可用"):
        build_analysis(ws, AnalysisRequest(selectedModel="data", cursorHour=8))
    with pytest.raises(ValueError, match="不能超过"):
        build_analysis(ws, AnalysisRequest(selectedModel="physics", cursorHour=21))
    with pytest.raises(ValueError, match="窑次已结束"):
        build_analysis(
            ws,
            AnalysisRequest(
                selectedModel="physics", cursorHour=20, parameters={"remainingDurationScale": 1.1}
            ),
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"temperatureOffsetC": 101},
        {"remainingDurationScale": 0.4},
        {"temperatureOffsetC": float("nan")},
        {"remainingDurationScale": float("inf")},
        {"loadKg": 900},
    ],
)
def test_parameter_bounds_and_unsupported_fields(parameters):
    with pytest.raises(ValidationError):
        AnalysisRequest(selectedModel="fusion", cursorHour=2, parameters=parameters)


def test_missing_future_plan_rejects_adjustment():
    ws = workspace()
    ws["points"][-1]["tempSp"] = None
    with pytest.raises(ValueError, match="未来设定温度计划不完整"):
        build_analysis(
            ws,
            AnalysisRequest(
                selectedModel="physics", cursorHour=8, parameters={"remainingDurationScale": 1.1}
            ),
        )


def test_prediction_gap_at_cursor_is_not_reported_as_zero_gas():
    ws = workspace()
    ws["points"][49]["physics"] = None
    with pytest.raises(ValueError, match="当前时刻附近缺少模型输入"):
        build_analysis(ws, AnalysisRequest(selectedModel="physics", cursorHour=8.25))


def test_short_windows_and_partial_interval_integration():
    points = [{"hour": 0, "gas": 0}, {"hour": 1, "gas": 100}]
    assert integrate_points(points, "gas", 0.25, 0.75) == 25
    result = build_analysis(workspace(), AnalysisRequest(selectedModel="physics", cursorHour=19.9))
    assert all(w["coveredMinutes"] == 6 for w in result["windows"])
    assert len({w["gas"] for w in result["windows"]}) == 1


def test_only_strictly_earlier_cycles_train_and_missing_temperature_is_unavailable():
    start = datetime(2026, 10, 1)
    target = sample(2, start)
    peers = [sample(1, start - timedelta(days=3)), sample(3, start + timedelta(days=3))]
    ws = build_workspace(target, peers)
    assert ws["trainCount"] == 1
    for p in target.points:
        p["temp_sp"] = p["zone_temp"] = None
    missing = build_workspace(target, [])
    assert not next(m for m in missing["models"] if m["key"] == "fusion")["available"]
    assert all(p["fusion"] is None for p in missing["points"])


def test_dashboard_rejects_executable_components_and_binds_required_panels():
    import json

    from api.services.kiln_reports import parse_dashboard
    from common.errors import AppError

    payload = {
        "title": "用气分析",
        "summary": "基于所选模型分析。",
        "kpis": ["remainingGas", "wholeGas", "remainingGas"],
        "panels": [
            {"kind": "temperature", "title": "温度", "explanation": "设定计划"},
            {"kind": "windows", "title": "用气窗口", "explanation": "模型积分"},
        ],
        "insights": ["关注温区偏差"],
        "limitations": ["历史回放试算"],
    }
    result = parse_dashboard(json.dumps(payload), adjusted=True)
    assert result["kpis"] == ["remainingGas", "wholeGas"]
    assert {"process", "gas", "comparison"}.issubset({p["kind"] for p in result["panels"]})
    payload["panels"][0]["kind"] = "customHtml"
    with pytest.raises(AppError, match="格式无效"):
        parse_dashboard(json.dumps(payload), adjusted=False)
