"""算法接入发布必须走真实训练验证，未发布或失配实现不得进入业务实验。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from test_kiln_algorithm_registry import registry as _registry
from test_kiln_mechanism_catalog import studio_db as _studio_db
from test_kiln_studio import cycles as _cycles
from test_kiln_studio import parsed as _parsed

import common.kiln_studio_engine as engine
from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router
from api.services import kiln_algorithm_management as management
from api.services import kiln_studio as studio
from common.errors import AppError
from common.kiln_algorithm_release import (
    AlgorithmBindRequest,
    AlgorithmDraftRequest,
    AlgorithmPublishRequest,
    AlgorithmRetireRequest,
    AlgorithmValidationRequest,
    implementation_fingerprint,
)
from common.kiln_studio_schema import ExperimentRequest, PhysicsParameters

registry, studio_db, cycles, parsed = _registry, _studio_db, _cycles, _parsed


async def setup(db, definition, cycles):
    engine.register_algorithm(*definition)
    project = await studio.create_asset(db, 7, "", "project", "测试资料项目", {})
    dataset = await studio.create_asset(
        db,
        7,
        project.public_id,
        "dataset",
        "测试真实训练数据",
        {
            "config": {"zoneCount": 0},
            "dataKey": studio.write_json(7, cycles),
            "dataHash": "test-data-hash",
            "sourceKind": "synthetic",
        },
    )
    mechanism = await studio.create_asset(
        db,
        7,
        project.public_id,
        "mechanism",
        "测试机理",
        {
            "datasetId": dataset.public_id,
            "parameters": PhysicsParameters().model_dump(),
            "templateKeys": ["thermal"],
            "version": "test-mechanism",
        },
    )
    draft = await management.create_draft(
        db, 7, AlgorithmDraftRequest(name="新增训练方法测试", requestedKey=definition[0].key)
    )
    config = ExperimentRequest(
        datasetId=dataset.public_id,
        mechanismId=mechanism.public_id,
        task="temperature",
        historyMinutes=5,
        horizonMinutes=5,
        calibrate=False,
        selectedAlgorithmKeys=[definition[0].key],
    )
    return draft["id"], AlgorithmValidationRequest(projectId=project.public_id, config=config)


async def bind(db, release, definition):
    return await management.bind(
        db,
        7,
        release,
        AlgorithmBindRequest(implementationKey=definition.key, version=definition.version),
    )


async def checked(db, release, definition, request):
    await bind(db, release, definition)
    job = await management.start_validation(db, 7, release, request)
    await studio.execute_job(job["id"], 7)
    db.session.expire_all()
    return await studio.get_asset(db, 7, release, management.RELEASE)


async def test_full_lifecycle_trains_restores_publishes_and_retires(studio_db, registry, cycles):
    async with studio_db() as db:
        release, body = await setup(db, registry, cycles)
        key = registry[0].key
        assert key not in {a["key"] for a in await studio.list_algorithm_catalog(db, 7)}
        with pytest.raises(AppError, match="绑定"):
            await management.start_validation(db, 7, release, body)
        row = await checked(db, release, registry[0], body)
        assert row.payload["state"] == "validated"
        report = await studio.get_asset(db, 7, row.payload["validationId"], management.CHECK)
        assert report.payload["technicalPassed"]
        assert report.payload["validation"]["count"] > 0
        assert report.payload["restoreMaxDifference"] == 0
        assert key not in {a["key"] for a in await studio.list_algorithm_catalog(db, 7)}
        await management.publish(
            db,
            7,
            release,
            AlgorithmPublishRequest(
                validationId=report.public_id, notes="仅确认本次温度测试的接入能力"
            ),
        )
        assert key in {a["key"] for a in await studio.list_algorithm_catalog(db, 7)}
        assert key not in {a["key"] for a in await studio.list_algorithm_catalog(db, 8)}
        assert (await studio.selected_algorithm_definitions(db, 7, [key]))[0].version == registry[
            0
        ].version
        await management.retire(db, 7, release, AlgorithmRetireRequest(reason="停止新实验"))
        assert key not in {a["key"] for a in await studio.list_algorithm_catalog(db, 7)}
        with pytest.raises(AppError):
            await studio.selected_algorithm_definitions(db, 7, [key])
        artifact = studio.read_json(7, report.payload["artifactKey"])
        assert engine.predict_model(artifact, {}) is not None
        assert len((await studio.get_asset(db, 7, release)).payload["history"]) == 6


async def test_no_relabeling_builtins_or_binding_missing_code(studio_db):
    async with studio_db() as db:
        draft = await management.create_draft(db, 7, AlgorithmDraftRequest(name="新算法需求"))
        for key in ("ridge", "not_deployed"):
            with pytest.raises(AppError):
                await management.bind(
                    db, 7, draft["id"], AlgorithmBindRequest(implementationKey=key, version="1")
                )
        with pytest.raises(AppError):
            await management.bind(
                db,
                8,
                draft["id"],
                AlgorithmBindRequest(implementationKey="not_deployed", version="1"),
            )
        with pytest.raises(AppError):
            await management.publish(
                db,
                7,
                draft["id"],
                AlgorithmPublishRequest(validationId="fake", notes="不能跳过验证"),
            )


async def test_runtime_change_invalidates_publishing_and_catalog(
    studio_db, registry, cycles, monkeypatch
):
    async with studio_db() as db:
        release, request = await setup(db, registry, cycles)
        row = await checked(db, release, registry[0], request)
        original = deepcopy(row.payload)
        key = registry[0].key
        monkeypatch.setitem(
            engine._ALGORITHMS_BY_KEY, key, registry[0].model_copy(update={"version": "test2"})
        )
        with pytest.raises(AppError, match="版本"):
            await management.publish(
                db,
                7,
                release,
                AlgorithmPublishRequest(
                    validationId=original["validationId"], notes="旧验证不可复用"
                ),
            )
        state = await management.management_state(db, 7)
        assert state["items"][0]["runtimeIssue"]
        assert key not in await management.published_keys(db, 7)


async def test_bad_prediction_fails_real_job_and_cannot_publish(
    studio_db, registry, cycles, monkeypatch
):
    definition, impl = registry
    monkeypatch.setitem(
        engine._IMPLEMENTATIONS, definition.key, impl._replace(predict=lambda *args: None)
    )
    # setup 会注册，使用异常推理回调模拟真实接入失败，不伪造验证报告。
    async with studio_db() as db:
        release, request = await setup(
            db, (definition, impl._replace(predict=lambda *args: None)), cycles
        )
        row = await checked(db, release, definition, request)
        assert row.payload["state"] == "failed"
        assert row.payload["validationId"] is None
        job = await studio.get_asset(db, 7, row.payload["latestJobId"], "job")
        assert job.payload["status"] == "failed"
        assert job.payload["error"]


async def test_validation_checks_selected_key_duplicate_jobs_and_dataset_ownership(
    studio_db, registry, cycles
):
    async with studio_db() as db:
        release, request = await setup(db, registry, cycles)
        await bind(db, release, registry[0])
        wrong = request.model_copy(
            update={
                "config": request.config.model_copy(update={"selectedAlgorithmKeys": ["ridge"]})
            }
        )
        with pytest.raises(AppError, match="只能"):
            await management.start_validation(db, 7, release, wrong)
        foreign = request.model_copy(update={"projectId": "missing"})
        with pytest.raises(AppError):
            await management.start_validation(db, 7, release, foreign)
        await management.start_validation(db, 7, release, request)
        with pytest.raises(AppError, match="正在运行"):
            await management.start_validation(db, 7, release, request)


async def test_normal_experiment_requires_queue_fingerprint(studio_db, registry, cycles):
    async with studio_db() as db:
        release, request = await setup(db, registry, cycles)
        row = await checked(db, release, registry[0], request)
        await management.publish(
            db,
            7,
            release,
            AlgorithmPublishRequest(
                validationId=row.payload["validationId"], notes="测试新实验链路"
            ),
        )
        job = await studio.new_job(
            db,
            7,
            request.projectId,
            "experiment",
            request.config.model_dump(),
            algorithm_definitions=[registry[0].model_dump(mode="json")],
        )
        await studio.execute_job(job["id"], 7)
        db.session.expire_all()
        assert (await studio.get_asset(db, 7, job["id"])).payload["status"] == "failed"
        fingerprint = implementation_fingerprint(registry[0])
        task = await studio.get_asset(db, 7, job["id"])
        task.payload = {
            **task.payload,
            "implementationFingerprints": {registry[0].key: fingerprint},
        }
        await db.commit()
        await studio.execute_job(task.public_id, 7)
        db.session.expire_all()
        assert (await studio.get_asset(db, 7, task.public_id)).payload["status"] == "succeeded"


async def test_management_http_permissions_and_invalid_request(studio_db):
    from api.services.menus import ADMIN_ONLY_MENUS, ALL_MENUS

    assert "/algorithm-manage" in ADMIN_ONLY_MENUS
    assert ALL_MENUS.count("/algorithm-manage") == 1
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
            assert (await client.get("/kiln-studio/algorithm-management")).status_code == 403
            assert (
                await client.post("/kiln-studio/algorithm-management", json={"name": "x"})
            ).status_code == 403
            current.is_superuser = True
            for body in ({"name": " "}, {"name": "算法", "code": "print('not allowed')"}):
                assert (
                    await client.post("/kiln-studio/algorithm-management", json=body)
                ).status_code == 422
            response = await client.post(
                "/kiln-studio/algorithm-management", json={"name": "新训练方法"}
            )
            assert response.status_code == 200
            release = response.json()["data"]["id"]
            assert (
                await client.get(f"/kiln-studio/algorithm-management/{release}/job")
            ).status_code == 404
