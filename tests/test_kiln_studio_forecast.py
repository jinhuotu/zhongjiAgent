import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import exp
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from common.kiln_studio_comparison import compare_temperature_plans, temperature_comparison_report
from common.kiln_studio_forecast import run_temperature_forecast
from common.kiln_studio_schema import ExperimentRequest, TemperatureForecastRequest


def fixture():
    start = datetime(2026, 1, 1)
    rows = [
        {
            "time": minute,
            "ts": (start + timedelta(minutes=minute)).isoformat(),
            "cycle": "c1",
            "temp": 500,
            "z1_temp": 400,
            "temp_sp": 600,
            "gas": 20,
            "air": 200,
            "pressure": 5,
            "o2": 2,
        }
        for minute in range(0, 125, 5)
    ]
    cycle = {"id": "c1", "name": "cycle.xls", "points": rows}
    artifact = {
        "algorithm": "physics",
        "parameters": {"tauMinutes": 30},
        "config": ExperimentRequest(
            datasetId="d", mechanismId="m", task="temperature"
        ).model_dump(),
    }
    return cycle, artifact


def request(**overrides):
    return TemperatureForecastRequest(cycleId="c1", **{"cursorFraction": 0.5, **overrides})


def test_saved_model_computes_trajectory_without_future_readings():
    cycle, model = fixture()
    baseline = run_temperature_forecast(cycle, model, request(), zone_count=1)
    changed = deepcopy(cycle)
    for point in changed["points"][13:]:
        point.update(temp=1000, gas=200, z1_temp=900)
    forecast = run_temperature_forecast(changed, model, request(), zone_count=1)
    assert forecast["metrics"]["pointCount"] == 12
    assert forecast["points"][18]["forecast"] == pytest.approx(600 - 100 * exp(-1))
    assert [point["forecast"] for point in forecast["points"]] == [
        point["forecast"] for point in baseline["points"]
    ]
    assert forecast["points"][13]["actualAfter"] == 1000
    assert forecast["points"][13]["actual"] is None
    assert forecast["points"][13]["gas"] is None
    assert forecast["points"][13]["zoneTemperatures"] == []


def test_delay_ramp_and_tau_change_scenario_without_modifying_saved_parameters():
    cycle, model = fixture()
    forecast = run_temperature_forecast(
        cycle, model, request(offsetC=40, delayMinutes=7, rampMinutes=8)
    )
    result = forecast["points"]
    assert result[13]["scenario"] == pytest.approx(result[13]["forecast"])
    assert result[14]["adjustedSetpoint"] == 615
    assert result[15]["adjustedSetpoint"] == 640
    assert result[15]["scenario"] > result[15]["forecast"]
    slower = run_temperature_forecast(cycle, model, request(tauMinutes=60))
    assert slower["metrics"]["scenarioEndTemperature"] < slower["metrics"]["endTemperature"]
    assert model["parameters"]["tauMinutes"] == 30


def test_missing_plan_stops_curve_and_preserves_alert_interval():
    cycle, model = fixture()
    cycle["points"][18]["temp_sp"] = None
    forecast = run_temperature_forecast(cycle, model, request(deviationC=20, deviationMinutes=10))
    assert forecast["points"][17]["forecast"] is not None
    assert all(point["forecast"] is None for point in forecast["points"][18:])
    alert = forecast["alerts"][0]
    assert (alert["start"], alert["end"]) == (65, 85)
    assert alert["temperature"] - alert["setpoint"] == pytest.approx(alert["deviation"])
    assert forecast["notes"]


def test_saved_fixed_horizon_model_uses_replay_and_one_selected_origin_point():
    cycle, model = fixture()
    model["algorithm"] = "baseline"
    result = run_temperature_forecast(cycle, model, request())
    assert result["mode"] == "rolling"
    assert sum(point["rolling"] is not None for point in result["points"]) > 1
    assert [point["minute"] for point in result["points"] if point["forecast"] is not None] == [90]
    with pytest.raises(ValueError, match="固定时长"):
        run_temperature_forecast(cycle, model, request(offsetC=10))


def test_zone_model_uses_the_saved_zone_observation():
    cycle, model = fixture()
    model["config"]["zone"] = 1
    forecast = run_temperature_forecast(cycle, model, request(), zone_count=1)
    assert forecast["targetLabel"] == "1 区"
    assert forecast["points"][12]["forecast"] == 400
    assert forecast["points"][12]["zoneTemperatures"] == [400]


def test_comparison_keeps_algorithm_and_mechanism_aligned_and_ignores_future_actuals():
    cycle, model = fixture()
    model["algorithm"] = "baseline"
    model["config"]["horizonMinutes"] = 10
    settings = request(
        comparePlans=True, durationMinutes=30, offsetC=40, rampMinutes=5, holdMinutes=5, targetC=560
    )
    result = compare_temperature_plans(cycle, model, settings, 1, thermal_enabled=True)
    points = {point["minute"]: point for point in result["points"]}
    assert result["comparison"]["algorithmPoints"] == 30
    assert result["comparison"]["algorithmTrajectory"] == "persistence"
    assert [points[minute]["algorithmForecast"] for minute in range(61, 91)] == [500] * 30
    assert points[70]["algorithmForecast"] == 500
    assert points[65]["adjustedSetpoint"] == 640
    assert points[75]["adjustedSetpoint"] == 600
    assert points[90]["scenario"] > points[90]["forecast"]
    assert result["comparison"]["endDeltaC"] == pytest.approx(
        points[90]["scenario"] - points[90]["forecast"]
    )
    assert result["comparison"]["commonAlgorithmActual"]["count"] == 6
    assert "共同实测" in str(temperature_comparison_report(result).findings)

    changed = deepcopy(cycle)
    for point in changed["points"][13:]:
        point.update(temp=1234, gas=99)
    other = compare_temperature_plans(changed, model, settings, 1, thermal_enabled=True)
    for field in ("forecast", "scenario", "algorithmForecast"):
        assert [p[field] for p in result["points"]] == [p[field] for p in other["points"]]
    assert result["comparison"]["algorithmActual"] != other["comparison"]["algorithmActual"]


def test_exclusion_shows_original_algorithm_and_does_not_reuse_removed_history():
    cycle, model = fixture()
    model["algorithm"] = "baseline"
    model["config"]["horizonMinutes"] = 10
    start = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))).timestamp() / 60
    for point in cycle["points"]:
        point["time"] += start
    settings = request(
        comparePlans=True,
        durationMinutes=30,
        excludedRange={"start": "2026-01-01T00:35", "end": "2026-01-01T00:55"},
    )
    result = compare_temperature_plans(cycle, model, settings, 1, thermal_enabled=True)
    points = {p["minute"]: p for p in result["points"]}
    assert result["comparison"]["excludedCount"] == 4
    assert result["comparison"]["algorithmPoints"] == 0
    assert points[70]["beforeExclusionAlgorithm"] == 500
    assert points[35]["actual"] is None
    assert cycle["points"][7]["temp"] == 500
    assert result["comparison"]["exclusionMaxDeltaC"] == 0


@pytest.mark.asyncio
async def test_report_reads_saved_forecast_without_rerunning_or_replacing_model(monkeypatch):
    from api.services import kiln_studio

    cycle, artifact = fixture()
    bundle = SimpleNamespace(
        project_id="project",
        payload={
            "datasetId": "dataset",
            "artifactKey": "model",
            "version": "v1",
        },
    )
    dataset = SimpleNamespace(payload={"dataKey": "data", "config": {"zoneCount": 1}})
    storage = {"model": [artifact], "data": [cycle]}
    assets = {"bundle": bundle, "dataset": dataset}

    async def get_asset(_db, owner, public_id, _kind):
        assert owner == 7
        return assets[public_id]

    async def create_asset(_db, owner, project, kind, _title, payload):
        assert (owner, project, kind) == (7, "project", "temperature_run")
        assets["run"] = SimpleNamespace(public_id="run", payload=payload)
        return assets["run"]

    def write(_owner, result):
        storage["result"] = deepcopy(result)
        return "result"

    monkeypatch.setattr(kiln_studio, "get_asset", get_asset)
    monkeypatch.setattr(kiln_studio, "create_asset", create_asset)
    monkeypatch.setattr(kiln_studio, "read_json", lambda _owner, key: storage[key])
    monkeypatch.setattr(kiln_studio, "write_json", write)
    build_llm = AsyncMock(side_effect=AssertionError("温度报告应直接由保存的事实生成"))
    monkeypatch.setattr(kiln_studio, "build_llm_client", build_llm)
    db = SimpleNamespace(commit=AsyncMock())
    forecast = await kiln_studio.forecast_temperature(db, 7, "bundle", request(offsetC=20))
    assert forecast == await kiln_studio.get_temperature_run(db, 7, "run")
    storage.clear()
    storage["result"] = {
        key: value for key, value in forecast.items() if key not in {"runId", "bundleVersion"}
    }
    report = await kiln_studio.temperature_report(db, 7, "run")
    assert report["runId"] == forecast["runId"]
    assert forecast["targetLabel"] in report["summary"]
    delta = forecast["metrics"]["scenarioEndTemperature"] - forecast["metrics"]["endTemperature"]
    assert f"升高 {delta:.2f} ℃" in report["findings"][0]
    assert "durationMinutes" not in str(report)
    build_llm.assert_not_awaited()

    # The workbench uses the same route and can reload the computed scenario and evidence.
    assets["bundle"].payload["manifest"] = [
        {
            "task": "temperature",
            "mechanismId": "mechanism",
            "experimentId": "experiment",
            "algorithmName": "机理基线",
            "mechanismVersion": "m1",
            "test": {"mae": 3, "rmse": 5, "count": 25},
        }
    ]
    assets["mechanism"] = SimpleNamespace(payload={"templateKeys": ["thermal"]})
    assets["experiment"] = SimpleNamespace(
        payload={"split": [{"name": "训练", "to": cycle["points"][5]["ts"]}]}
    )
    storage.update(model=[artifact], data=[cycle])
    comparison = await kiln_studio.forecast_temperature(
        db, 7, "bundle", request(comparePlans=True, offsetC=15, durationMinutes=30)
    )
    assert comparison["comparison"]["afterTraining"] is True
    assert comparison["comparison"]["historicalTest"]["count"] == 25
    assert comparison == await kiln_studio.get_temperature_run(db, 7, "run")
    report = await kiln_studio.temperature_report(db, 7, "run")
    assert any("综合温差 5.00 ℃" in statement for statement in report["findings"])


@pytest.mark.asyncio
async def test_dashboard_layout_matches_saved_bundle_tasks(monkeypatch):
    from api.services import kiln_studio
    from common.errors import AppError
    from common.kiln_studio_schema import DashboardLayoutRequest

    bundle = SimpleNamespace(payload={"manifest": [{"task": "energy"}]})
    get_asset = AsyncMock(return_value=bundle)
    complete = AsyncMock(return_value='{"task":"energy","chart":"bar","windowMinutes":5}')
    monkeypatch.setattr(kiln_studio, "get_asset", get_asset)
    monkeypatch.setattr(
        kiln_studio, "build_llm_client", AsyncMock(return_value=SimpleNamespace(complete=complete))
    )
    db = SimpleNamespace(commit=AsyncMock())
    body = DashboardLayoutRequest(prompt="看未来五分钟的累计燃气柱状图")
    layout = await kiln_studio.generate_dashboard_layout(db, 7, "bundle", body)
    get_asset.assert_awaited_once_with(db, 7, "bundle", "bundle")
    assert layout.model_dump() == {"task": "energy", "chart": "bar", "windowMinutes": 5}
    context = json.loads(complete.call_args.args[0][1]["content"])
    assert context == {"availableTasks": ["energy"], "request": body.prompt}

    complete.return_value = '{"task":"temperature","chart":"bar","windowMinutes":5}'
    with pytest.raises(AppError, match="未包含"):
        await kiln_studio.generate_dashboard_layout(db, 7, "bundle", body)


def test_temperature_observations_route_only_accepts_cycles_in_the_dataset(monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from api.deps import get_current_user
    from api.routers.kiln_studio import router
    from api.services import kiln_studio
    from common.errors import AppError
    from db.session import get_db

    app = FastAPI()
    app.include_router(router)
    db = object()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7)
    app.add_exception_handler(
        AppError, lambda _request, exc: JSONResponse({"message": exc.msg}, exc.status_code)
    )
    dataset = SimpleNamespace(
        payload={
            "config": {"zoneCount": 2},
            "summary": [{"fileId": "c", "file": "record.csv"}],
            "dataKey": "records",
        },
    )
    get_asset = AsyncMock(return_value=dataset)
    read_json = Mock(
        return_value=[{"id": "c", "points": [{"time": 0, "z1_temp": 620, "z2_temp": 610}]}]
    )
    monkeypatch.setattr(kiln_studio, "get_asset", get_asset)
    monkeypatch.setattr(kiln_studio, "read_json", read_json)

    with TestClient(app) as client:
        path = "/kiln-studio/datasets/dataset/temperature-observations"
        assert client.get(path, params={"cycle_id": "other"}).status_code == 422
        read_json.assert_not_called()
        response = client.get(path, params={"cycle_id": "c"})
        assert response.status_code == 200
        assert response.json()["data"]["points"] == [
            {"timestampMs": 0, "temperatures": [620, 610]},
        ]
    get_asset.assert_awaited_with(db, 7, "dataset", "dataset")


def test_uploaded_record_is_checked_saved_and_used_to_forecast(monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from api.deps import get_current_user
    from api.routers.kiln_studio import router
    from api.services import kiln_studio
    from common.errors import AppError
    from db.session import get_db

    _, artifact = fixture()
    app = FastAPI()
    app.include_router(router)
    db = SimpleNamespace(commit=AsyncMock())
    bundle = dict(
        datasetId="dataset",
        artifactKey="model",
        version="v1",
        manifest=[{**artifact, "task": "temperature"}],
    )
    assets = {
        "bundle": SimpleNamespace(project_id="project", payload=bundle, kind="bundle"),
        "other": SimpleNamespace(project_id="project", payload=bundle, kind="bundle"),
        "dataset": SimpleNamespace(
            kind="dataset",
            payload={
                "config": {"zoneCount": 1, "kilnCode": "test-kiln"},
                "dataKey": "training-data-is-not-read",
            },
        ),
    }

    async def get_asset(_db, owner, public_id, kind):
        assert owner == 7
        value = assets[public_id]
        assert kind == value.kind
        return value

    async def create_asset(_db, owner, project, kind, _title, payload, public_id=None):
        assert (owner, project) == (7, "project")
        key = public_id or "run"
        assets[key] = SimpleNamespace(public_id=key, payload=payload, project_id=project, kind=kind)
        return assets[key]

    async def list_assets(_db, owner, kind, project, *, bundle_id):
        assert (owner, kind, project) == (7, "temperature_input", "project")
        return [
            row.payload
            for row in assets.values()
            if row.kind == kind and row.payload["bundleId"] == bundle_id
        ]

    async def mock_db():
        yield db

    async def mock_user():
        return SimpleNamespace(id=7)

    @app.exception_handler(AppError)
    async def handle_error(_request, exc):
        return JSONResponse({"msg": exc.msg}, status_code=exc.status_code)

    storage = {"model": [artifact]}

    def write_blob(_owner, key, value):
        storage[key] = value
        return key

    def write_json(_owner, value):
        return write_blob(_owner, f"json-{len(storage)}", deepcopy(value))

    monkeypatch.setattr(kiln_studio, "get_asset", get_asset)
    monkeypatch.setattr(kiln_studio, "create_asset", create_asset)
    monkeypatch.setattr(kiln_studio, "list_assets", list_assets)
    monkeypatch.setattr(kiln_studio, "read_json", lambda _owner, key: storage[key])
    monkeypatch.setattr(kiln_studio, "write_json", write_json)
    monkeypatch.setattr(kiln_studio, "write_blob", write_blob)
    app.dependency_overrides[get_db] = mock_db
    app.dependency_overrides[get_current_user] = mock_user
    path = "/kiln-studio/bundles/bundle/temperature-records"
    start = datetime(2026, 11, 1)
    rows = "\n".join(f"{start + timedelta(minutes=5 * i)},{510 + i},660" for i in range(10))
    files = {"file": ("新增窑次.csv", f"时间,实测温度,计划目标\n{rows}".encode(), "text/csv")}

    with TestClient(app) as client:
        preview = client.post(path + "/preview", files=files).json()["data"]
        assert preview["issues"] and preview["stats"]["setpointCount"] == 0
        assert set(storage) == {"model"}
        settings = {
            "fileHash": preview["fileHash"],
            "acknowledgeInputs": True,
            "useStandard": False,
            "mappings": [*preview["mappings"], {"column": "0:2", "field": "temp_sp"}],
        }
        mismatched = client.post(
            path,
            files=files,
            data={
                "settings": json.dumps({**settings, "fileHash": "0" * 64}),
            },
        )
        assert mismatched.status_code == 422
        response = client.post(path, files=files, data={"settings": json.dumps(settings)})
        assert response.status_code == 200, response.text
        record = response.json()["data"]
        assert record["count"] == record["setpointCount"] == 10
        assert client.get(path).json()["data"]["items"] == [record]
        assert client.get(path.replace("/bundle/", "/other/")).json()["data"]["items"] == []
        body = {
            "cycleId": record["fileId"],
            "cycleSource": "upload",
            "cursorFraction": record["recommendedCursorFraction"],
        }
        base = "/kiln-studio/bundles"
        assert client.post(f"{base}/other/temperature-forecast", json=body).status_code == 422
        predicted = client.post(f"{base}/bundle/temperature-forecast", json=body)
        assert predicted.status_code == 200, predicted.text
        result = predicted.json()["data"]
        assert result["cycleName"] == "新增窑次.csv"
        assert result["inputRecord"] == record
        assert result["metrics"]["pointCount"] > 1
        assert result["points"][5]["timestamp"].startswith("2026-11-01")
        assert client.get("/kiln-studio/temperature-runs/run").json()["data"] == result
