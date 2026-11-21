"""受控新算法真实训练、参数契约、历史复现和可选 ONNX 转换测试。"""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pydantic import Field
from test_kiln_mechanism_catalog import studio_db as _studio_db
from test_kiln_studio import cycles as _cycles
from test_kiln_studio import parsed as _parsed

import common.kiln_onnx_graph as graphs
import common.kiln_studio_engine as engine
from api.services import kiln_studio as studio
from common.kiln_studio_schema import (
    AlgorithmPresetRequest,
    AlgorithmTuning,
    ExperimentRequest,
    PhysicsParameters,
    Strict,
)

cycles, parsed, studio_db = _cycles, _parsed, _studio_db
KEY = "test_training_mean"


class MeanParameters(Strict):
    bias: float = Field(
        default=0, ge=-10, le=10, title="修正偏置", description="仅用于注册机制的自动化测试"
    )


@pytest.fixture
def registry(monkeypatch):
    # 测试算法只存在于当前测试的注册表，不能进入实际算法库或污染其他测试。
    monkeypatch.setattr(engine, "_IMPLEMENTATIONS", dict(engine._IMPLEMENTATIONS))
    monkeypatch.setattr(engine, "_ALGORITHMS_BY_KEY", dict(engine._ALGORITHMS_BY_KEY))
    monkeypatch.setattr(graphs, "_CUSTOM_CONVERTERS", dict(graphs._CUSTOM_CONVERTERS))
    definition = next(a for a in engine.algorithm_library() if a.key == "ridge").model_copy(
        update={
            "key": KEY,
            "name": "注册训练机制测试（合成数据）",
            "version": "test1",
            "oneLine": "仅在自动化测试中使用训练集均值",
            "baseKey": None,
            "tuning": None,
        }
    )

    def fit(x, y, config, parameters, train):
        return {
            "mean": float(y.mean()),
            "bias": config.algorithmOptions[KEY]["bias"],
            "trainingCount": len(train),
        }

    def predict(artifact, example):
        return artifact["model"]["mean"] + artifact["model"]["bias"]

    implementation = engine.AlgorithmImplementation(
        predict, fit, parameter_model=MeanParameters, tasks=("temperature",)
    )
    return definition, implementation


def request(**kwargs):
    return ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        historyMinutes=5,
        horizonMinutes=5,
        selectedAlgorithmKeys=[KEY],
        calibrate=False,
        **kwargs,
    )


def train(cycles, config):
    return engine.run_experiment(cycles, config, PhysicsParameters().model_dump(), ["thermal"])


def test_registered_algorithm_trains_and_restores_json_artifact(registry, cycles):
    definition, impl = registry
    engine.register_algorithm(definition, impl)
    result = train(cycles, request(algorithmOptions={KEY: {"bias": 2}}))
    samples = engine.examples(cycles, request(), PhysicsParameters().minGas)
    training, _, _ = engine.split_examples(samples)[0]
    expected = float(np.mean([row["y"] for row in training])) + 2
    artifact = json.loads(json.dumps(result["artifacts"][KEY], allow_nan=False))
    assert engine.predict_model(artifact, {}) == pytest.approx(expected)
    assert artifact["config"]["algorithmOptions"][KEY] == {"bias": 2}
    assert result["algorithms"][0]["test"]["count"] > 0
    info = next(a for a in engine.algorithm_library() if a.key == KEY)
    assert info.sourceKind == "builtin"
    assert info.capabilities.trainingMode == "trainable"
    assert info.capabilities.minuteForecast.status == "unsupported"
    assert info.capabilities.onnxExport.status == "unsupported"
    assert info.capabilities.pythonExport.status == "unsupported"
    assert info.parameterSchema["properties"]["bias"]["maximum"] == 10
    wrong = deepcopy(artifact)
    wrong["algorithmVersion"] = "old-version"
    with pytest.raises(ValueError, match="历史实现版本不匹配"):
        engine.predict_model(wrong, {})


def test_parameter_validation_defaults_unknown_fields_and_task_support(registry, cycles):
    definition, impl = registry
    engine.register_algorithm(definition, impl)
    for options in ({KEY: {"bias": 11}}, {KEY: {"invented": 1}}, {"not_selected": {"bias": 1}}):
        with pytest.raises(ValueError):
            train(cycles, request(algorithmOptions=options))
    normalized = engine.validate_algorithm_options(request(), [definition])
    assert normalized.algorithmOptions[KEY]["bias"] == 0
    assert "不支持" in engine.algorithm_unavailable_reason(KEY, "energy", [], False)


def test_duplicate_registration_and_nonserializable_weights_fail(registry, cycles):
    definition, impl = registry
    with pytest.raises(ValueError, match="保留"):
        engine.register_algorithm(definition.model_copy(update={"key": "onnx"}), impl)
    engine.register_algorithm(definition, impl._replace(fit=lambda *args: np.array([1, 2])))
    with pytest.raises(ValueError, match="重复"):
        engine.register_algorithm(definition, impl)
    with pytest.raises(ValueError, match="JSON"):
        train(cycles, request())


def test_registered_converter_is_used_and_numerically_verified(registry, cycles):
    onnx = pytest.importorskip("onnx")
    from common.kiln_model_export import export_numeric_model
    from common.kiln_onnx_export import export_onnx

    definition, impl = registry

    def convert(artifact):
        base = {**artifact, "algorithm": "baseline"}
        graph = onnx.load_model_from_string(graphs.build_onnx(base))
        graph.graph.node[-1].output[0] = "base_value"
        for name, value in [("zero", 0), ("mean", impl.predict(artifact, {}))]:
            graph.graph.initializer.append(
                onnx.numpy_helper.from_array(np.asarray(value, dtype=np.float32), name)
            )
        graph.graph.node.extend(
            [
                onnx.helper.make_node("Mul", ["base_value", "zero"], ["zero_value"]),
                onnx.helper.make_node("Add", ["zero_value", "mean"], ["predictions"]),
            ]
        )
        return graph.SerializeToString()

    engine.register_algorithm(definition, impl, onnx_exporter=convert)
    result = train(cycles, request())
    data = export_numeric_model(
        {
            "id": "test",
            "projectId": "test",
            "title": definition.name,
            "version": "test1",
            "manifest": [{"algorithmName": definition.name, "unit": "℃"}],
        },
        [result["artifacts"][KEY]],
        {"zoneCount": 0},
    )
    package = export_onnx(data, cycles)
    assert package["validation"]["models"][0]["status"] == "passed"
    assert (
        next(a for a in engine.algorithm_library() if a.key == KEY).capabilities.onnxExport.status
        == "supported"
    )


async def test_catalog_old_presets_gain_current_capabilities_without_rewriting(studio_db):
    async with studio_db() as db:
        item = await studio.save_algorithm_preset(
            db,
            7,
            AlgorithmPresetRequest(
                name="实际参数方案", baseKey="ridge", tuning=AlgorithmTuning(ridgeAlpha=2)
            ),
        )
        assert item["sourceKind"] == "preset"
        assert item["capabilities"]["trainingMode"] == "trainable"
        saved = await studio.get_asset(db, 7, item["key"], studio.ALGORITHM_PRESET_KIND)
        payload = deepcopy(saved.payload)
        catalog = await studio.list_algorithm_catalog(db, 7)
        assert saved.payload == payload
        assert any(a["key"] == item["key"] and a["sourceKind"] == "preset" for a in catalog)
        assert not any(a["key"] == KEY for a in catalog)


def test_registered_sequence_and_path_are_used_by_replay_and_prediction(registry, cycles):
    from common.kiln_export_example import model_example
    from common.kiln_studio_forecast import run_temperature_forecast
    from common.kiln_studio_replay import build_replay_package
    from common.kiln_studio_schema import ReplayRequest, TemperatureForecastRequest

    cycles = [{**cycle, "name": f"test-{index}.csv"} for index, cycle in enumerate(cycles)]
    definition, impl = registry

    def fit(*args):
        return {**impl.fit(*args), "offsets": [1, 2, 3, 4, 5], "sequenceLength": 16}

    def path(artifact, example):
        assert np.asarray(example["sequence"]).shape == (16, 7)
        mean = impl.predict(artifact, example)
        return {step: mean + step for step in artifact["model"]["offsets"]}

    engine.register_algorithm(
        definition,
        impl._replace(
            fit=fit, sequence=True, predict_path=path, predict=lambda a, e: path(a, e)[5]
        ),
    )
    result = train(cycles, request())
    artifact = json.loads(json.dumps(result["artifacts"][KEY]))
    assert result["algorithms"][0]["predictionOffsets"] == [1, 2, 3, 4, 5]
    replay = ReplayRequest(
        experimentId="e", cycleId=cycles[0]["id"], selectedAlgorithms=[KEY], horizonMinutes=10
    )
    first = build_replay_package(cycles, {KEY: artifact}, replay, 0)
    assert first.algorithms[0].forecastTrajectory == "multiOutput"
    series = next(
        frame.future[KEY]
        for frame in first.frames
        if all(point.value is not None for point in frame.future[KEY])
    )
    assert len(series) == 10
    # 单点预测、场景试算、真实输入示例也必须按注册契约构造历史序列。
    engine.simulate(cycles, [artifact], 0.5, 0)
    run_temperature_forecast(
        cycles[0],
        artifact,
        TemperatureForecastRequest(cycleId=cycles[0]["id"], cursorFraction=0.5),
        zone_count=0,
    )
    assert len(model_example(artifact, cycles, 0)["sequence"]) == 16
    invalid = deepcopy(artifact)
    invalid["model"]["offsets"] = [5, 1, 1]
    with pytest.raises(ValueError, match="offsets"):
        engine.prediction_offsets(invalid)


def test_http_rejects_invalid_algorithm_parameters_before_creating_job(registry, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from api.deps import get_current_user, get_db
    from api.routers.kiln_studio import router
    from common.errors import AppError

    definition, impl = registry
    engine.register_algorithm(definition, impl)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    @app.exception_handler(AppError)
    async def error_handler(_, exc):
        return JSONResponse({"msg": exc.msg}, status_code=exc.status_code)

    get_asset = AsyncMock(
        return_value=SimpleNamespace(project_id="p", public_id="d", payload={"datasetId": "d"})
    )
    monkeypatch.setattr(studio, "get_asset", get_asset)
    monkeypatch.setattr(
        studio, "selected_algorithm_definitions", AsyncMock(return_value=[definition])
    )
    new_job = AsyncMock()
    monkeypatch.setattr(studio, "new_job", new_job)
    with TestClient(app) as client:
        response = client.post(
            "/kiln-studio/projects/p/experiments",
            json=request(algorithmOptions={KEY: {"bias": 11}}).model_dump(),
        )
        assert response.status_code == 422
        assert "参数无效" in response.json()["msg"]
        new_job.assert_not_called()
        contract = client.get("/kiln-studio/algorithm-library/external-contract").json()["data"]
        assert contract["supportsSequence"] is False
        assert contract["supportsMultipleHorizons"] is False


def test_trusted_module_loader_and_flat_parameter_contract(registry, monkeypatch):
    from common import kiln_studio_plugins as plugins

    definition, impl = registry

    class NestedParameters(Strict):
        values: list[float]

    with pytest.raises(ValueError, match="平铺"):
        engine.register_algorithm(definition, impl._replace(parameter_model=NestedParameters))

    class AliasedParameters(Strict):
        value: float = Field(default=0, alias="renamed")

    with pytest.raises(ValueError, match="别名"):
        engine.register_algorithm(definition, impl._replace(parameter_model=AliasedParameters))
    calls = []
    monkeypatch.setattr(plugins, "ALGORITHM_MODULES", ("common.test_only_plugin",))
    monkeypatch.setattr(
        plugins, "import_module", lambda name: SimpleNamespace(register=lambda: calls.append(name))
    )
    plugins.install_algorithms()
    assert calls == ["common.test_only_plugin"]
    monkeypatch.setattr(plugins, "ALGORITHM_MODULES", ("uploads.untrusted",))
    with pytest.raises(ValueError, match="受控"):
        plugins.install_algorithms()
