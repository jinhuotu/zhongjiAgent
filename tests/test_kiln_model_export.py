"""真实训练产物的导出契约、鉴权和 NumPy 独立环境一致性测试。"""

import importlib.metadata
import json
import os
import shutil
import subprocess
import venv
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from test_kiln_mechanism_catalog import studio_db as _studio_db

from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router
from api.services import kiln_studio as studio
from common.errors import AppError
from common.kiln_export_example import build_export_example
from common.kiln_export_runtime import PYTHON_RUNTIME
from common.kiln_model_export import (
    export_numeric_model,
    export_python,
    export_skill,
    zip_python_files,
)
from common.kiln_studio_engine import algorithm_library, predict_model, run_experiment
from common.kiln_studio_schema import AlgorithmTuning, ExperimentRequest, PhysicsParameters

studio_db = _studio_db


@pytest.fixture(scope="module")
def trained_models():
    start = datetime(2026, 1, 1)
    cycle = {
        "id": "test-cycle",
        "name": "合成训练测试.csv",
        "points": [
            {
                "time": minute,
                "ts": (start + timedelta(minutes=minute)).isoformat(),
                "cycle": "test-cycle",
                "temp": 400 + minute * 0.2,
                "temp_sp": 500 + minute * 0.1,
                "gas": 30 + minute * 0.03,
                "air": 600 + minute * 0.2,
                "o2": 2,
                "pressure": 3,
            }
            for minute in range(260)
        ],
    }
    models, manifest = [], []
    for task, template in [("temperature", "thermal"), ("energy", "energy"), ("afr", "combustion")]:
        definitions = list(algorithm_library(enabled_only=True))
        if task == "temperature":
            ridge = next(item for item in definitions if item.key == "ridge")
            definitions.append(
                ridge.model_copy(
                    update={
                        "key": "ridge_custom",
                        "baseKey": "ridge",
                        "tuning": AlgorithmTuning(ridgeAlpha=0.8),
                    }
                )
            )
        config = ExperimentRequest(
            datasetId="d",
            mechanismId="m",
            task=task,
            historyMinutes=5,
            horizonMinutes=5,
            calibrate=False,
            boostingRounds=5,
            selectedAlgorithmKeys=[item.key for item in definitions],
        )
        result = run_experiment(
            [cycle], config, PhysicsParameters().model_dump(), [template], definitions=definitions
        )
        for card in result["algorithms"]:
            if not card["available"]:
                continue
            models.append(result["artifacts"][card["key"]])
            manifest.append(
                {
                    "task": task,
                    "algorithm": card["key"],
                    "algorithmName": card["name"],
                    "unit": result["unit"],
                    "config": config.model_dump(),
                    "validation": card["validation"],
                    "test": card["test"],
                }
            )
    bundle = {
        "id": "test-bundle",
        "projectId": "test-project",
        "title": "自动化测试（合成训练数据）",
        "version": "test-trained",
        "manifest": manifest,
    }
    data = export_numeric_model(bundle, models, {"zoneCount": 0})
    return data, [cycle]


def runtime():
    namespace = {"__name__": "offline_test"}
    exec(compile(PYTHON_RUNTIME, "predict.py", "exec"), namespace)
    return namespace


def test_offline_inference_matches_every_registered_implementation(trained_models):
    data, cycles = trained_models
    example = build_export_example(data, cycles)
    assert {model.get("implementationKey", model["algorithm"]) for model in data["models"]} == {
        "baseline",
        "physics",
        "ridge",
        "lasso",
        "boosting",
        "fusion",
        "lstm",
    }
    predict = runtime()["predict"]
    for model, item in zip(data["models"], example["inputs"], strict=True):
        # JSON 对象排序不同不应改变按 features 顺序构造的向量。
        item["features"] = dict(reversed(list(item["features"].items())))
        assert predict(model, item) == pytest.approx(item["expected"], rel=1e-10, abs=1e-9)


def test_missing_values_and_missing_feature_keys_are_handled(trained_models):
    data, cycles = trained_models
    example = build_export_example(data, cycles)
    predict = runtime()["predict"]
    for model, original in zip(data["models"], example["inputs"], strict=True):
        item = deepcopy(original)
        for index in (5, 6, 7, 8):
            item["features"][model["features"][index]] = None
        values = [item["features"][name] for name in model["features"]]
        ex = {
            "x": values,
            "y0": values[0],
            "sp0": values[9],
            "sp1": values[10],
            "slope": values[11],
        }
        if "sequence" in item:
            item["sequence"][0][3] = None
            ex["sequence"] = item["sequence"]
        assert predict(model, item) == pytest.approx(predict_model(model, ex), rel=1e-10, abs=1e-9)
        del item["features"][model["features"][0]]
        with pytest.raises(ValueError, match="缺少特征"):
            predict(model, item)


@pytest.fixture(scope="session")
def numpy_only_python(tmp_path_factory):
    folder = tmp_path_factory.mktemp("numpy-only-runtime")
    venv.EnvBuilder(with_pip=False).create(folder)
    library = folder / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{os.sys.version_info.major}.{os.sys.version_info.minor}/site-packages"
    )
    source = Path(np.__file__).parent
    shutil.copytree(source, library / "numpy")
    for dependency in (
        source.parent / "numpy.libs",
        Path(importlib.metadata.distribution("numpy")._path),
    ):
        if dependency.exists():
            shutil.copytree(dependency, library / dependency.name)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata as m,json;"
            "print(json.dumps([d.metadata['Name'] for d in m.distributions()]))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["numpy"]
    return python


def test_zip_runs_with_only_numpy_installed(trained_models, numpy_only_python, tmp_path):
    data, cycles = trained_models
    example = build_export_example(data, cycles)
    package = export_python(data, example)
    zip_bytes = zip_python_files(package)
    with ZipFile(BytesIO(zip_bytes)) as archive:
        assert set(archive.namelist()) == {
            "model.json",
            "predict.py",
            "README.md",
            "input_example.json",
            "requirements.txt",
        }
        archive.extractall(tmp_path)
    result = subprocess.run(
        [str(numpy_only_python), "-I", str(tmp_path / "predict.py"), "--verify"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    outputs = json.loads(result.stdout)
    assert len(outputs) == len(data["models"])
    for output, item in zip(outputs, example["inputs"], strict=True):
        assert output["value"] == pytest.approx(item["expected"], rel=1e-10, abs=1e-9)


def test_skill_contains_exact_model_package_and_real_contracts(trained_models):
    data, _ = trained_models
    skill = export_skill(data)
    assert skill["modelPackage"] == data
    assert skill["name"] == data["bundle"]["title"]
    assert skill["version"] == data["bundle"]["version"]
    for index, contract in enumerate(skill["inputs"]):
        assert [field["name"] for field in contract["inputs"]] == data["models"][index]["features"]
        assert contract["output"]["unit"] == data["bundle"]["manifest"][index]["unit"]
    # 燃气任务可能保留页面上的温区选择，目标仍应为总燃气流量。
    zoned = deepcopy(data)
    for model in zoned["models"]:
        model["config"]["zone"] = 3
    for contract in export_skill(zoned)["inputs"]:
        if "sequence" in contract:
            expected = {"temperature": "z3_temp", "energy": "gas", "afr": "afr"}[contract["task"]]
            assert contract["sequence"]["channels"][0] == expected


def test_external_onnx_is_preserved_as_json_but_rejected_for_numpy_package(trained_models):
    data, _ = deepcopy(trained_models)
    data["models"][0]["implementationKey"] = "onnx"
    assert export_skill(data)["modelPackage"] == data
    with pytest.raises(AppError, match="onnx"):
        export_python(data, {"inputs": []})


async def saved_bundle(studio_db, trained_models):
    data, cycles = trained_models
    async with studio_db() as db:
        dataset = await studio.create_asset(
            db,
            7,
            "project",
            "dataset",
            "合成测试数据",
            {
                "config": data["datasetConfig"],
                "dataKey": studio.write_json(7, cycles),
            },
        )
        bundle = await studio.create_asset(
            db,
            7,
            "project",
            "bundle",
            data["bundle"]["title"],
            {
                "datasetId": dataset.public_id,
                "version": "test-version",
                "manifest": data["bundle"]["manifest"],
                "artifactKey": studio.write_json(7, data["models"]),
            },
        )
        await db.commit()
        return bundle.public_id


async def test_export_reads_assets_once_and_preserves_default_json(
    studio_db, trained_models, monkeypatch
):
    bundle_id = await saved_bundle(studio_db, trained_models)
    async with studio_db() as db:
        raw = await studio.export_bundle(db, 7, bundle_id)
        assert set(raw) == {
            "format",
            "bundle",
            "models",
            "datasetConfig",
            "engineVersion",
            "replayInstructions",
        }
        assert raw["format"] == "kiln-studio-numeric-model-v1"
        assert await studio.export_bundle(db, 7, bundle_id, "json") == raw
        lookup = AsyncMock(wraps=studio.get_asset)
        monkeypatch.setattr(studio, "get_asset", lookup)
        package = await studio.export_bundle(db, 7, bundle_id, "python")
        assert lookup.await_count == 2
        assert (
            json.loads(
                next(file["content"] for file in package["files"] if file["path"] == "model.json")
            )
            == raw
        )
        with pytest.raises(AppError, match="支持 json"):
            await studio.export_bundle(db, 7, bundle_id, "exe")


async def test_http_formats_zip_authorization_and_validation(studio_db, trained_models):
    bundle_id = await saved_bundle(studio_db, trained_models)
    app = FastAPI()
    app.include_router(router)
    user = SimpleNamespace(id=7)

    async def database():
        async with studio_db() as db:
            yield db

    async def current_user():
        return user

    @app.exception_handler(AppError)
    async def error_handler(request, exc):
        return JSONResponse({"code": exc.code, "msg": exc.msg}, status_code=exc.status_code)

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current_user
    base = f"/kiln-studio/bundles/{bundle_id}/export"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        raw = (await client.get(base)).json()["data"]
        assert (await client.get(base, params={"format": "json"})).json()["data"] == raw
        assert (await client.get(base, params={"format": "skill"})).json()["data"][
            "modelPackage"
        ] == raw
        assert (await client.get(base, params={"format": "python"})).json()["data"]["files"]
        invalid = await client.get(base, params={"format": "exe"})
        assert invalid.status_code == 422
        assert all(fmt in invalid.text for fmt in ("json", "skill", "python"))
        response = await client.get(base + "/python.zip")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "test-version.zip" in response.headers["content-disposition"]
        with ZipFile(BytesIO(response.content)) as archive:
            assert json.loads(archive.read("model.json")) == raw
        user.id = 8
        assert (await client.get(base)).status_code == 404
        assert (await client.get(base + "/python.zip")).status_code == 404
