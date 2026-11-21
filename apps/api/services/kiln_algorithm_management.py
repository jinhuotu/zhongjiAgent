"""算法包接入、受控绑定、真实训练验证和账号内发布。"""

import asyncio
import json
from datetime import UTC, datetime
from time import perf_counter

import numpy as np
from sqlalchemy import select

from api.services import kiln_studio as studio
from common.errors import AppError, ErrorCode
from common.kiln_algorithm_packages import predict_package, save_definition
from common.kiln_algorithm_release import builtin_keys, implementation_fingerprint
from common.kiln_studio_engine import (
    algorithm_implementation,
    algorithm_library,
    examples,
    predict_many,
    predict_model,
    predict_path,
    prediction_offsets,
    run_experiment,
    split_examples,
    validate_algorithm_options,
)
from common.kiln_studio_schema import AlgorithmMetadata
from db.models.kiln_studio import KilnStudioAsset

RELEASE = "algorithm_release"
CHECK = "algorithm_check"


def invalid(message, status=422):
    raise AppError(ErrorCode.CONFLICT if status == 409 else ErrorCode.VALIDATION, message, status)


def registered(key, version=None):
    definition = next((item for item in algorithm_library() if item.key == key), None)
    if definition is None:
        invalid("实现未部署；请由研发加入受控模块清单并部署 API/实验执行进程")
    if key in builtin_keys():
        invalid("这是已有内置算法，请使用参数方案；新增算法必须绑定独立的新实现")
    impl = algorithm_implementation(key)
    if not impl or not impl.fit:
        invalid("新增可训练算法必须提供真实 fit 和 predict 实现")
    if version and definition.version != version:
        invalid("部署的实现版本已变化，请重新绑定并验证", 409)
    return definition


def bound_definition(payload):
    if not payload.get("implementationKey"):
        invalid("尚未绑定真实算法实现")
    snapshot = payload.get("definition") or {}
    if snapshot.get("package"):
        definition = AlgorithmMetadata.model_validate(snapshot)
        if (definition.key, definition.version) != (payload["implementationKey"], payload["implementationVersion"]):
            invalid("绑定的算法包版本不一致", 409)
    else:
        definition = registered(payload["implementationKey"], payload["implementationVersion"])
    try:
        fingerprint = implementation_fingerprint(definition)
    except ValueError as exc:
        invalid(str(exc))
    if fingerprint != payload["implementationFingerprint"]:
        invalid("实现代码或运行契约已变化，请新建版本并重新验证", 409)
    return definition


def event(action, detail):
    return {"action": action, "detail": detail, "at": datetime.now(UTC).isoformat()}


async def locked_release(db, owner, release_id):
    row = (
        await db.execute(
            select(KilnStudioAsset)
            .where(
                KilnStudioAsset.public_id == release_id,
                KilnStudioAsset.user_id == owner,
                KilnStudioAsset.kind == RELEASE,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "算法记录不存在或无权访问", 404)
    return row


async def management_state(db, owner):
    implementations = []
    for item in algorithm_library():
        impl = algorithm_implementation(item.key)
        implementations.append(
            {
                "definition": item.model_dump(mode="json"),
                "builtin": item.key in builtin_keys(),
                "bindable": item.key not in builtin_keys() and bool(impl and impl.fit),
            }
        )
    rows = await studio.list_assets(db, owner, RELEASE)
    for row in rows:
        row["runtimeIssue"] = ""
        if row.get("implementationKey"):
            try:
                bound_definition(row)
            except (AppError, ValueError) as exc:
                row["runtimeIssue"] = str(exc)
    return {"items": rows, "implementations": implementations, "scope": "current_account"}


async def create_draft(db, owner, body):
    row = await studio.create_asset(
        db,
        owner,
        "",
        RELEASE,
        body.name,
        {
            **body.model_dump(),
            "state": "draft",
            "implementationKey": None,
            "implementationVersion": None,
            "implementationFingerprint": None,
            "definition": None,
            "validationId": None,
            "activeJobId": None,
            "history": [event("created", "登记新算法；等待绑定受控实现")],
        },
    )
    await db.commit()
    return studio.asset_dict(row)


def require_bindable(payload):
    if payload["state"] not in {"draft", "bound", "failed", "validated"}:
        invalid("验证中或已发布的版本不能覆盖，请新建算法版本", 409)


def binding_payload(payload, definition, action):
    try:
        fingerprint = implementation_fingerprint(definition)
    except ValueError as exc:
        invalid(str(exc))
    return {
        **payload,
        "state": "bound",
        "implementationKey": definition.key,
        "implementationVersion": definition.version,
        "implementationFingerprint": fingerprint,
        "definition": definition.model_dump(mode="json"),
        "validationId": None,
        "activeJobId": None,
        "latestJobId": None,
        "history": [*payload["history"], event(action, f"{definition.key} · {definition.version}")],
    }


async def bind(db, owner, release_id, body):
    row = await locked_release(db, owner, release_id)
    require_bindable(row.payload)
    definition = registered(body.implementationKey, body.version)
    row.payload = binding_payload(row.payload, definition, "bound")
    await db.commit()
    return studio.asset_dict(row)


async def upload_package(db, owner, blob, release_id=None):
    if release_id:
        require_bindable((await studio.get_asset(db, owner, release_id, RELEASE)).payload)
    package_id = release_id or studio.uid()
    try:
        definition = await asyncio.to_thread(save_definition, blob, f"package_{package_id}")
    except (ValueError, OSError) as exc:
        invalid(str(exc) if isinstance(exc, ValueError) else "无法保存算法包，请联系管理员检查存储")
    if release_id:
        row = await locked_release(db, owner, release_id)
        require_bindable(row.payload)
        row.payload = binding_payload(row.payload, definition, "package_uploaded")
    else:
        payload = binding_payload(
            {"name": definition.name, "description": definition.explanation, "requestedKey": None, "history": []},
            definition,
            "package_uploaded",
        )
        row = await studio.create_asset(db, owner, "", RELEASE, definition.name, payload, public_id=package_id)
    await db.commit()
    return studio.asset_dict(row)


async def validation_inputs(db, owner, project_id, config):
    await studio.get_asset(db, owner, project_id, "project")
    dataset = await studio.get_asset(db, owner, config.datasetId, "dataset")
    mechanism = await studio.get_asset(db, owner, config.mechanismId, "mechanism")
    if (
        dataset.project_id != project_id
        or mechanism.project_id != project_id
        or mechanism.payload["datasetId"] != dataset.public_id
    ):
        invalid("验证项目、数据版本和机理版本不匹配")
    if config.zone > dataset.payload["config"]["zoneCount"]:
        invalid("预测温区超出所选数据版本范围")
    return dataset, mechanism


async def start_validation(db, owner, release_id, body):
    row = await locked_release(db, owner, release_id)
    if row.payload["state"] not in {"bound", "failed", "validated", "validating"}:
        invalid("请先绑定实现；已发布/停用版本需新建记录后验证", 409)
    active = row.payload.get("activeJobId")
    if active:
        job = await studio.get_asset(db, owner, active, "job")
        if job.payload["status"] in {"queued", "running"}:
            invalid("该算法已有验证任务正在运行", 409)
    definition = bound_definition(row.payload)
    if body.config.selectedAlgorithmKeys != [definition.key]:
        invalid("验证必须且只能选择当前绑定的新算法")
    config = validate_algorithm_options(body.config, [definition])
    await validation_inputs(db, owner, body.projectId, config)
    job = await studio.new_job(
        db,
        owner,
        body.projectId,
        "algorithm_validate",
        {
            "releaseId": release_id,
            "fingerprint": row.payload["implementationFingerprint"],
            "config": config.model_dump(),
        },
        algorithm_definitions=[definition.model_dump(mode="json")],
        commit=False,
    )
    row.payload = {
        **row.payload,
        "state": "validating",
        "activeJobId": job["id"],
        "latestJobId": job["id"],
        "validationId": None,
        "history": [*row.payload["history"], event("validation", job["id"])],
    }
    await db.commit()
    return job


def calculate_validation(cycles, config, parameters, templates, definition):
    """用真实训练/留出窗口验证可执行性；效果是否满足业务仍由发布者确认。"""
    started = perf_counter()
    result = run_experiment(cycles, config, parameters, templates, [definition])
    artifact = result["artifacts"].get(definition.key)
    if artifact is None:
        raise ValueError(result["algorithms"][0]["note"] or "没有生成有效模型")
    restored = json.loads(json.dumps(artifact, allow_nan=False))
    impl = algorithm_implementation(definition.key)
    rows = examples(
        cycles,
        config,
        parameters["minGas"],
        include_sequence=bool(impl and impl.sequence),
        include_trajectory=config.task in {"temperature", "energy"},
    )
    _, _, heldout = split_examples(rows)[0]
    probes = heldout[: min(10, len(heldout))]
    first, second = predict_many(artifact, probes), predict_many(restored, probes)
    if not all(value is not None and np.isfinite(value) for value in [*first, *second]):
        raise ValueError("留出试跑缺少有效预测，请检查字段和输出")
    if not np.allclose(first, second, rtol=1e-10, atol=1e-10):
        raise ValueError("JSON 保存/恢复后的推理结果不一致")
    singles = (
        predict_package(restored, probes, each=True) if definition.package
        else [predict_model(restored, example) for example in probes]
    )
    if any(value is None for value in singles) or not np.allclose(
        singles, second, rtol=1e-7, atol=1e-7
    ):
        raise ValueError("批量推理和单点推理结果不一致")
    offsets = prediction_offsets(restored)
    if impl and impl.predict_path:
        for example, single in zip(probes, singles, strict=True):
            path = predict_path(restored, example)
            if set(path) != set(offsets) or not np.isclose(path[config.horizonMinutes], single):
                raise ValueError("多时距输出与声明覆盖或单点预测不一致")
    card = result["algorithms"][0]
    if not card["validation"]["count"] or not card["test"]["count"]:
        raise ValueError("验证/测试集没有有效输出")
    return {
        "status": "passed",
        "technicalPassed": True,
        "validation": card["validation"],
        "test": card["test"],
        "splitMethod": result["splitMethod"],
        "predictionOffsets": offsets,
        "probeCount": len(probes),
        "restoreMaxDifference": float(np.max(np.abs(np.asarray(first) - second))),
        "elapsedSeconds": perf_counter() - started,
        "artifact": restored,
        "limitations": [
            "验证通过表示接入可执行，不代表业务精度达标。",
            "训练回调只接收训练窗口；代码内部是否另读未来数据仍需研发审查。",
            "本次未验证 ONNX/Python 交付、量化或边缘硬件性能。",
        ],
    }


async def execute_validation(db, owner, job):
    from common.kiln_studio_schema import ExperimentRequest

    body = job.payload["body"]
    row = await studio.get_asset(db, owner, body["releaseId"], RELEASE)
    definition = bound_definition(row.payload)
    if row.payload.get("activeJobId") != job.public_id:
        invalid("验证任务已被更新，不能覆盖新结果", 409)
    if body["fingerprint"] != row.payload["implementationFingerprint"]:
        invalid("验证绑定的实现已经变化", 409)
    config = ExperimentRequest.model_validate(body["config"])
    dataset, mechanism = await validation_inputs(db, owner, job.project_id, config)
    await studio._job_update(db, job, progress=20, message="真实训练与留出评估，核对保存后推理")
    report = await asyncio.to_thread(
        calculate_validation,
        studio.read_json(owner, dataset.payload["dataKey"]),
        config,
        mechanism.payload["parameters"],
        mechanism.payload["templateKeys"],
        definition,
    )
    # 等待期间若实现或版本发生改变，结果不能成为可发布证据。
    bound_definition(row.payload)
    artifact_key = studio.write_json(owner, report.pop("artifact"))
    check = await studio.create_asset(
        db,
        owner,
        job.project_id,
        CHECK,
        row.title,
        {
            **report,
            "releaseId": row.public_id,
            "fingerprint": body["fingerprint"],
            "implementationKey": definition.key,
            "implementationVersion": definition.version,
            "config": config.model_dump(),
            "datasetId": dataset.public_id,
            "dataHash": dataset.payload["dataHash"],
            "mechanismId": mechanism.public_id,
            "mechanismVersion": mechanism.payload["version"],
            "artifactKey": artifact_key,
        },
    )
    row.payload = {
        **row.payload,
        "state": "validated",
        "validationId": check.public_id,
        "activeJobId": None,
        "history": [*row.payload["history"], event("validated", check.public_id)],
    }
    await db.commit()
    return check.public_id


async def fail_validation(db, owner, job, message):
    row = await studio.get_asset(db, owner, job.payload["body"]["releaseId"], RELEASE)
    if row.payload.get("activeJobId") == job.public_id:
        row.payload = {
            **row.payload,
            "state": "failed",
            "activeJobId": None,
            "validationId": None,
            "history": [*row.payload["history"], event("failed", message[:600])],
        }


async def publish(db, owner, release_id, body):
    row = await locked_release(db, owner, release_id)
    if row.payload["state"] != "validated" or row.payload["validationId"] != body.validationId:
        invalid("必须使用当前版本最近一次通过的验证结果发布", 409)
    definition = bound_definition(row.payload)
    if definition.status != "enabled":
        invalid("该实现仍为试验/停用状态，研发确认启用后须重新绑定并验证")
    report = await studio.get_asset(db, owner, body.validationId, CHECK)
    if (
        report.payload["releaseId"] != release_id
        or not report.payload["technicalPassed"]
        or report.payload["fingerprint"] != row.payload["implementationFingerprint"]
    ):
        invalid("验证结果与当前实现不匹配", 409)
    rows = (
        (
            await db.execute(
                select(KilnStudioAsset)
                .where(
                    KilnStudioAsset.user_id == owner,
                    KilnStudioAsset.kind == RELEASE,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if any(
        other.public_id != release_id
        and other.payload["state"] == "published"
        and other.payload["implementationKey"] == definition.key
        for other in rows
    ):
        invalid("该实现已有发布记录，请使用独立的新版本键或先停用原记录", 409)
    row.payload = {
        **row.payload,
        "state": "published",
        "publishedAt": datetime.now(UTC).isoformat(),
        "history": [*row.payload["history"], event("published", body.notes.strip())],
    }
    await db.commit()
    return studio.asset_dict(row)


async def retire(db, owner, release_id, body):
    row = await locked_release(db, owner, release_id)
    if row.payload["state"] != "published":
        invalid("仅已发布版本可以停用", 409)
    row.payload = {
        **row.payload,
        "state": "retired",
        "history": [*row.payload["history"], event("retired", body.reason.strip())],
    }
    await db.commit()
    return studio.asset_dict(row)


async def published_definitions(db, owner, *, historical=False):
    """Return account-scoped releases whose exact packages or registered versions remain available."""
    rows = (
        (
            await db.execute(
                select(KilnStudioAsset).where(
                    KilnStudioAsset.user_id == owner,
                    KilnStudioAsset.kind == RELEASE,
                )
            )
        )
        .scalars()
        .all()
    )
    definitions = []
    states = {"published", "retired"} if historical else {"published"}
    for row in rows:
        if row.payload["state"] not in states:
            continue
        try:
            definitions.append(bound_definition(row.payload))
        except (AppError, ValueError):
            continue
    return definitions


async def published_keys(db, owner):
    return {item.key for item in await published_definitions(db, owner)}
