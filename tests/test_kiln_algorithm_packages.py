import json
import os
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from test_kiln_mechanism_catalog import studio_db as _studio_db
from test_kiln_studio import cycles as _cycles
from test_kiln_studio import parsed as _parsed
from test_kiln_studio_replay import example as replay_example

from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router
from api.services import kiln_studio as studio
from common import kiln_algorithm_packages as packages
from common import kiln_studio_replay
from common.errors import AppError
from common.kiln_algorithm_package_example import example_package
from common.kiln_studio_schema import PhysicsParameters

studio_db, cycles, parsed = _studio_db, _cycles, _parsed
PATH = "/kiln-studio/algorithm-management"
ZIP = {"Content-Type": "application/zip"}


def test_example_package_and_bad_member():
    manifest, sources = packages.read_package(example_package())
    assert manifest.tasks == ["temperature"]
    assert "def fit(" in sources["algorithm.py"]
    archive = BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("algorithm.json", manifest.model_dump_json())
        output.writestr("../algorithm.py", sources["algorithm.py"])
    with pytest.raises(ValueError, match="ZIP 根目录"):
        packages.read_package(archive.getvalue())


def test_uploaded_model_replay_batches_native_horizon(monkeypatch):
    name = "package_test"
    records, artifacts, request = replay_example((name,), 10)
    artifacts[name]["algorithmPackage"] = {"zip": "test"}
    artifacts[name]["config"]["horizonMinutes"] = 5
    calls = []

    def predict(artifact, samples):
        calls.append(len(samples))
        return [row["y0"] + 2 for row in samples]

    monkeypatch.setattr(kiln_studio_replay, "predict_many", predict)
    replay = kiln_studio_replay.build_replay_package(records, artifacts, request, 1)
    assert len(calls) == 1 and calls[0] > 1
    assert replay.algorithms[0].forecastTrajectory == "fixedHorizon"
    frame = next(frame for frame in replay.frames if frame.originMinute >= 20)
    assert [point.minuteOffset for point in frame.future[name] if point.value is not None] == [5]


def test_prediction_protocol_omits_held_out_observations(monkeypatch, tmp_path):
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(packages, "configured_image", lambda: image)
    monkeypatch.setattr(
        packages,
        "get_settings",
        lambda: SimpleNamespace(
            storage_root=tmp_path,
            kiln_algorithm_runner_image=image,
        ),
    )
    definition = packages.save_definition(example_package(), "package_test")
    assert definition.parameterSchema["properties"]["scale"]["default"] == 1

    def execute(image_id, payload):
        assert image_id == image
        request = json.loads(payload)
        assert request["operation"] == "predict_many"
        assert request["body"]["rows"] == [{"x": [12, 10], "y0": 12}]
        assert "datasetId" not in request["body"]["artifact"]["config"]
        assert "algorithmPackage" not in request["body"]["artifact"]
        return [13.0]

    monkeypatch.setattr(packages, "_execute", execute)
    artifact = {
        "algorithm": definition.key,
        "algorithmPackage": definition.package.model_dump(),
        "config": {
            "task": "temperature",
            "zone": 0,
            "historyMinutes": 5,
            "horizonMinutes": 5,
            "datasetId": "account-snapshot",
            "algorithmOptions": {definition.key: {"scale": 1}},
        },
        "model": {},
        "preprocessor": {},
        "parameters": {},
        "features": [],
    }
    assert packages.predict_package(
        artifact,
        [
            {
                "x": [12, 10],
                "y0": 12,
                "y": 20,
                "trajectoryTargets": [18, 20],
            }
        ],
    ) == [13.0]
    packages._path(definition.package.sha256).write_bytes(b"modified")
    with pytest.raises(ValueError, match="指纹不一致"):
        packages.read_verified(definition.package)


async def test_upload_and_publish_with_real_docker(studio_db, cycles, monkeypatch, tmp_path):
    image = os.getenv("KILN_ALGORITHM_TEST_IMAGE")
    if not image:
        pytest.skip("set KILN_ALGORITHM_TEST_IMAGE to the built Linux runner image")
    monkeypatch.setattr(
        packages,
        "get_settings",
        lambda: SimpleNamespace(
            storage_root=tmp_path,
            kiln_algorithm_runner_image=image,
        ),
    )
    app = FastAPI()
    app.include_router(router)
    current = SimpleNamespace(id=7, is_superuser=False, roles=[])
    app.dependency_overrides[get_current_user] = lambda: current

    @app.exception_handler(AppError)
    async def handle(_, exc):
        return JSONResponse({"msg": exc.msg}, status_code=exc.status_code)

    async with studio_db() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            url = f"{PATH}/packages"
            assert (
                await client.post(url, headers=ZIP, content=example_package())
            ).status_code == 403
            current.is_superuser = True
            assert (
                await client.post(url, headers=ZIP, content=b"a" * (packages.MAX_PACKAGE + 1))
            ).status_code == 413
            assert (await client.post(url, headers=ZIP, content=b"invalid ZIP")).status_code == 422
            template = await client.get(f"{url}/example")
            assert template.status_code == 200
            draft = (await client.post(PATH, json={"name": "已有算法登记"})).json()["data"]
            current.id = 8
            blocked = await client.post(
                url,
                params={"release_id": draft["id"]},
                headers=ZIP,
                content=template.content,
            )
            assert blocked.status_code == 404
            current.id = 7
            attached = await client.post(
                url,
                params={"release_id": draft["id"]},
                headers=ZIP,
                content=template.content,
            )
            assert attached.status_code == 200, attached.text
            assert attached.json()["data"]["id"] == draft["id"]
            assert attached.json()["data"]["state"] == "bound"
            uploaded = await client.post(url, headers=ZIP, content=template.content)
            assert uploaded.status_code == 200, uploaded.text
            release = uploaded.json()["data"]
            assert release["state"] == "bound"
            assert release["definition"]["sourceKind"] == "package"
            definition = release["definition"]
            assert definition["key"] not in {
                row["key"] for row in await studio.list_algorithm_catalog(db, 7)
            }
            project = await studio.create_asset(db, 7, "", "project", "验证项目", {})
            dataset = await studio.create_asset(
                db,
                7,
                project.public_id,
                "dataset",
                "验证数据",
                {
                    "config": {"zoneCount": 0},
                    "dataKey": studio.write_json(7, cycles),
                    "dataHash": "demo",
                    "sourceKind": "synthetic",
                },
            )
            mechanism = await studio.create_asset(
                db,
                7,
                project.public_id,
                "mechanism",
                "验证机理",
                {
                    "datasetId": dataset.public_id,
                    "parameters": PhysicsParameters().model_dump(),
                    "templateKeys": ["thermal"],
                    "version": "test",
                },
            )
            await db.commit()
            config = {
                "datasetId": dataset.public_id,
                "mechanismId": mechanism.public_id,
                "task": "temperature",
                "calibrate": False,
                "historyMinutes": 5,
                "horizonMinutes": 5,
                "selectedAlgorithmKeys": [definition["key"]],
            }
            release_path = f"{PATH}/{release['id']}"
            started = await client.post(
                f"{release_path}/validate",
                json={
                    "projectId": project.public_id,
                    "config": config,
                },
            )
            assert started.status_code == 200, started.text
            db.session.expire_all()
            validation = (await client.get(f"{release_path}/validation")).json()["data"]["report"]
            assert validation is not None, (await client.get(f"{release_path}/job")).text
            assert validation["technicalPassed"] and validation["test"]["count"] > 0
            assert validation["restoreMaxDifference"] == 0
            published = await client.post(
                f"{release_path}/publish",
                json={
                    "validationId": validation["id"],
                    "notes": "通过隔离训练回归测试",
                },
            )
            assert published.status_code == 200, published.text
            assert definition["key"] in {
                row["key"] for row in await studio.list_algorithm_catalog(db, 7)
            }
            assert definition["key"] not in {
                row["key"] for row in await studio.list_algorithm_catalog(db, 8)
            }
            assert (
                await client.post(
                    url,
                    params={"release_id": release["id"]},
                    headers=ZIP,
                    content=template.content,
                )
            ).status_code == 409
            experiment = await client.post(
                f"/kiln-studio/projects/{project.public_id}/experiments", json=config
            )
            assert experiment.status_code == 200, experiment.text
            db.session.expire_all()
            job = await studio.get_asset(db, 7, experiment.json()["data"]["id"], "job")
            assert job.payload["status"] == "succeeded", job.payload.get("error")
