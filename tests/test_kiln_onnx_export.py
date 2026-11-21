"""标准 ONNX 图、逐分钟曲线、独立历史预处理及下载契约。"""

import importlib.metadata
import json
import os
import shutil
import subprocess
import venv
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import onnxruntime as ort
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from test_kiln_model_export import saved_bundle
from test_kiln_model_export import studio_db as _studio_db
from test_kiln_model_export import trained_models as _trained_models

from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router
from common.errors import AppError
from common.kiln_onnx_examples import reference_values
from common.kiln_onnx_export import TOLERANCE, export_onnx, zip_onnx_files
from common.kiln_onnx_graph import build_onnx, output_offsets
from common.kiln_onnx_preprocess import prepare
from common.kiln_studio_engine import make_features
from common.kiln_studio_observations import clean_rows
from common.kiln_studio_schema import ExperimentRequest

studio_db = _studio_db
trained_models = _trained_models
onnx = pytest.importorskip("onnx", reason="ONNX 导出专项测试需要 onnx-export 可选依赖")


@pytest.fixture(scope="session")
def onnx_runtime_python(tmp_path_factory):
    # 独立环境只复制运行依赖，不安装平台或 ONNX 构图包，也不修改旧 NumPy 测试环境。
    folder = tmp_path_factory.mktemp("onnx-only-runtime")
    venv.EnvBuilder(with_pip=False).create(folder)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    library = folder / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{os.sys.version_info.major}.{os.sys.version_info.minor}/site-packages"
    )
    source = Path(np.__file__).parent.parent
    for name in ("numpy", "onnxruntime", "packaging"):
        shutil.copytree(source / name, library / name)
        metadata = Path(importlib.metadata.distribution(name)._path)
        shutil.copytree(metadata, library / metadata.name)
    if (source / "numpy.libs").exists():
        shutil.copytree(source / "numpy.libs", library / "numpy.libs")
    check = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import onnxruntime,importlib.util;"
            "assert importlib.util.find_spec('onnx') is None;"
            "assert importlib.util.find_spec('common') is None;"
            "print(onnxruntime.__version__)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert check.stdout.strip() == ort.__version__
    return python


@pytest.fixture(scope="module")
def package(trained_models):
    data, cycles = trained_models
    return export_onnx(data, cycles)


def file_json(package, path):
    return json.loads(next(file["content"] for file in package["files"] if file["path"] == path))


def test_all_implementations_and_real_minute_heads_are_verified(package):
    manifest = file_json(package, "manifest.json")
    validation = file_json(package, "validation.json")
    assert validation["status"] == "passed"
    assert {m["implementation"] for m in manifest["models"]} == {
        "baseline",
        "physics",
        "ridge",
        "lasso",
        "boosting",
        "fusion",
        "lstm",
    }
    assert all(m["sampleCount"] > 1 and m["status"] == "passed" for m in validation["models"])
    for spec in manifest["models"]:
        if (
            spec["implementation"] in {"lstm", "ridge", "fusion"}
            and spec["config"]["task"] != "afr"
        ):
            assert spec["offsetsMinutes"] == [1, 2, 3, 4, 5]
    assert not manifest["targetHardwareVerified"]
    assert not manifest["quantized"]


def test_zip_contains_loadable_self_contained_onnx_and_no_platform_imports(
    package, tmp_path, onnx_runtime_python
):
    with ZipFile(BytesIO(zip_onnx_files(package))) as archive:
        archive.extractall(tmp_path)
        names = archive.namelist()
        assert all(
            file in names
            for file in (
                "manifest.json",
                "validation.json",
                "run.py",
                "preprocess.py",
                "sensor_input_example.json",
            )
        )
        for name in names:
            if name.endswith(".onnx"):
                model = onnx.load_model_from_string(archive.read(name))
                onnx.checker.check_model(model, full_check=True)
                assert model.ir_version == 8
                assert model.opset_import[0].version == 13
        assert "common." not in archive.read("run.py").decode()
        assert "common." not in archive.read("preprocess.py").decode()
    for input_name in ("input_example.json", "sensor_input_example.json"):
        result = subprocess.run(
            [
                str(onnx_runtime_python),
                "-I",
                str(tmp_path / "run.py"),
                "--input",
                str(tmp_path / input_name),
                "--verify",
            ],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        assert len(json.loads(result.stdout)) == len(file_json(package, "manifest.json")["models"])


def test_null_and_irregular_history_rules_match_and_preserve_batch(trained_models, package):
    data, _ = trained_models
    specs = file_json(package, "manifest.json")["models"]
    items = file_json(package, "input_example.json")["inputs"]
    sensors = file_json(package, "sensor_input_example.json")["inputs"]
    for spec, item, sensor in zip(specs, items, sensors, strict=True):
        explicit, historical = prepare(item, spec), prepare(sensor, spec)
        assert all(
            np.array_equal(explicit[key], historical[key], equal_nan=True) for key in explicit
        )
        model = data["models"][spec["modelIndex"]]
        session = ort.InferenceSession(build_onnx(model), providers=["CPUExecutionProvider"])
        assert session.run(None, explicit)[0].shape == (1, len(output_offsets(model)))
        # 真正制造缺测，核对图内填充与平台逻辑一致，而不只是核对正常输入。
        missing = deepcopy(item)
        for field in spec["features"][5:9]:
            missing["features"][field] = None
        x = [missing["features"][field] for field in spec["features"]]
        ex = {"x": x, "y0": x[0], "sp0": x[9], "sp1": x[10], "slope": x[11]}
        if "sequence" in missing:
            missing["sequence"][0][3] = None
            ex["sequence"] = missing["sequence"]
        expected = reference_values(model, ex, missing["futurePlans"])
        actual = session.run(None, prepare(missing, spec))[0][0]
        assert np.allclose(actual, expected, **TOLERANCE)
    item = deepcopy(items[0])
    item["features"].pop(specs[0]["features"][0])
    with pytest.raises(ValueError, match="缺少输入特征"):
        prepare(item, specs[0])
    item = deepcopy(items[0])
    item["features"][specs[0]["features"][0]] = "900"
    with pytest.raises(ValueError, match="数字"):
        prepare(item, specs[0])


def test_unsupported_models_cannot_silently_export(trained_models):
    data, cycles = deepcopy(trained_models)
    data["models"][0]["implementationKey"] = "onnx"
    with pytest.raises(AppError, match="暂不支持 onnx"):
        export_onnx(data, cycles)


def test_irregular_and_missing_history_matches_platform(package):
    spec = next(
        s
        for s in file_json(package, "manifest.json")["models"]
        if s["implementation"] == "lstm" and s["config"]["task"] == "energy"
    )
    sensor = deepcopy(file_json(package, "sensor_input_example.json")["inputs"][spec["modelIndex"]])
    history = sensor["history"]
    del history[2]
    history[0]["gas_total"] = -9999
    history[1]["temp"] = -9999
    cfg = ExperimentRequest.model_validate(spec["config"])
    rows = clean_rows(
        history, spec["zoneCount"], exclude_zero_temperature=cfg.excludeZeroTemperatureRecords
    )
    expected = make_features(rows, sensor["futurePlans"][-1], cfg, include_sequence=True)
    actual = prepare(sensor, spec)
    assert np.array_equal(
        actual["features"][0], np.asarray(expected["x"], dtype=np.float32), equal_nan=True
    )
    assert np.array_equal(
        actual["sequence"][0], np.asarray(expected["sequence"], dtype=np.float32), equal_nan=True
    )


def test_legacy_single_output_lstm_retains_its_native_horizon(package, trained_models):
    data, _ = trained_models
    spec = next(
        s for s in file_json(package, "manifest.json")["models"] if s["implementation"] == "lstm"
    )
    model = deepcopy(data["models"][spec["modelIndex"]])
    saved = model["model"]
    column = saved["offsets"].index(model["config"]["horizonMinutes"])
    for key in ("output", "context"):
        saved["weights"][key] = np.asarray(saved["weights"][key])[:, column].tolist()
    saved["weights"]["offset"] = saved["weights"]["offset"][column]
    saved.pop("offsets")
    sample = file_json(package, "input_example.json")["inputs"][spec["modelIndex"]]
    session = ort.InferenceSession(build_onnx(model), providers=["CPUExecutionProvider"])
    actual = session.run(None, prepare(sample, spec))[0]
    assert actual.shape == (1, 1)
    assert output_offsets(model) == [model["config"]["horizonMinutes"]]
    assert np.allclose(actual[0, 0], sample["expectedValues"][column], **TOLERANCE)


def test_mismatched_onnx_cannot_receive_a_passed_validation(trained_models, monkeypatch):
    import common.kiln_onnx_export as exporter

    data, cycles = trained_models
    original = exporter.build_onnx

    def wrong_graph(model):
        # 保持输入规格，只故意改变输出，确保测试的是数值核对而非张量形状检查。
        changed = onnx.load_model_from_string(original(model))
        changed.graph.node[-1].output[0] = "unaltered_predictions"
        changed.graph.initializer.append(
            onnx.numpy_helper.from_array(np.float32(1000), "test_bias")
        )
        changed.graph.node.append(
            onnx.helper.make_node("Add", ["unaltered_predictions", "test_bias"], ["predictions"])
        )
        return changed.SerializeToString()

    monkeypatch.setattr(exporter, "build_onnx", wrong_graph)
    with pytest.raises(AppError, match="逐点核对失败"):
        exporter.export_onnx(data, cycles)


async def test_onnx_download_capabilities_formats_and_ownership(studio_db, trained_models):
    bundle_id = await saved_bundle(studio_db, trained_models)
    app, user = FastAPI(), SimpleNamespace(id=7)
    app.include_router(router)

    async def database():
        async with studio_db() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user

    @app.exception_handler(AppError)
    async def handler(request, exc):
        return JSONResponse({"msg": exc.msg}, status_code=exc.status_code)

    base = f"/kiln-studio/bundles/{bundle_id}/export"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        capability = await client.get(base + "/capabilities")
        assert capability.json()["data"]["onnx"]["available"]
        listing = (await client.get(base, params={"format": "onnx"})).json()["data"]
        assert listing["format"] == "kiln-studio-onnx-package-v1"
        response = await client.get(base + "/onnx.zip")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "test-version.onnx.zip" in response.headers["content-disposition"]
        with ZipFile(BytesIO(response.content)) as archive:
            assert json.loads(archive.read("manifest.json"))["bundleVersion"] == "test-version"
        assert (await client.get(base + "/exe.zip")).status_code == 422
        user.id = 8
        assert (await client.get(base + "/capabilities")).status_code == 404
        assert (await client.get(base + "/onnx.zip")).status_code == 404
