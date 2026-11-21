from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, Path, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from api.deps import AdminUser, CurrentUser, DbSession
from api.services import kiln_algorithm_management as algorithm_management
from api.services import kiln_knowledge
from api.services import kiln_studio as service
from common.errors import AppError, ErrorCode
from common.kiln_algorithm_package_example import example_package
from common.kiln_algorithm_packages import MAX_PACKAGE
from common.kiln_algorithm_release import (
    AlgorithmBindRequest,
    AlgorithmDraftRequest,
    AlgorithmPublishRequest,
    AlgorithmRetireRequest,
    AlgorithmValidationRequest,
    builtin_keys,
    implementation_fingerprint,
)
from common.kiln_studio_engine import FEATURES
from common.kiln_studio_external import MAX_EXTERNAL_MODEL_BYTES
from common.kiln_studio_files import demo_archive
from common.kiln_studio_schema import (
    AlgorithmAvailabilityRequest,
    AlgorithmPresetRequest,
    AlgorithmRecommendationRequest,
    AlgorithmRecommendationResponse,
    BundleRequest,
    CatalogRevisionRequest,
    ConfirmRequest,
    ConfirmTemperatureRecord,
    DashboardLayoutRequest,
    DocumentRecognitionRequest,
    ExperimentRequest,
    ExternalAlgorithmImport,
    MechanismComparisonRequest,
    MechanismRequest,
    PredictionEvaluationRequest,
    ReplayPackage,
    ReplayRequest,
    SaveMechanismRequest,
    SimulateRequest,
    TemperatureForecastRequest,
)
from common.response import ApiResponse, ok

router = APIRouter(prefix="/kiln-studio", tags=["kiln-studio"])


@router.get("/projects")
async def projects(db: DbSession, user: CurrentUser):
    rows = await service.list_assets(db, user.id, "project")
    return ok(
        {"items": [{k: v for k, v in row.items() if k not in {"files", "uploads"}} for row in rows]}
    )


@router.post("/projects")
async def upload(
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),  # noqa: B008
    title: str = Form(default="", max_length=160),
):
    project_id, job = await service.upload_project(db, user.id, files, title)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok({"projectId": project_id, "job": job})


@router.get("/demo.zip")
async def demo(user: CurrentUser):
    return Response(
        demo_archive(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=kiln-synthetic-demo.zip"},
    )


@router.get("/projects/{project_id}")
async def project(project_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.project_detail(db, user.id, project_id))


@router.post("/projects/{project_id}/files")
async def append_project_files(
    project_id: str,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),  # noqa: B008
):
    job = await kiln_knowledge.append_files(db, user.id, project_id, files)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.delete("/projects/{project_id}/files/{file_id}")
async def delete_project_file(project_id: str, file_id: str, db: DbSession, user: CurrentUser):
    return ok(await kiln_knowledge.delete_file(db, user.id, project_id, file_id))


@router.get("/projects/{project_id}/files/{file_id}/download")
async def download_project_file(project_id: str, file_id: str, db: DbSession, user: CurrentUser):
    project = await service.get_asset(db, user.id, project_id, "project")
    file = kiln_knowledge.selected_files(project, [file_id])[0]
    if not file.get("rawKey"):
        raise AppError(ErrorCode.NOT_FOUND, "原文件不存在，请重新解析资料", 404)
    return Response(
        service._path(user.id, file["rawKey"]).read_bytes(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(PurePosixPath(file["path"]).name)
        },
    )


@router.post("/projects/{project_id}/files/{file_id}/reparse")
async def reparse_project_file(
    project_id: str,
    file_id: str,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
):
    job = await kiln_knowledge.queue_reparse(db, user.id, project_id, file_id)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.post("/projects/{project_id}/knowledge/recognize")
async def recognize_project_documents(
    project_id: str,
    body: DocumentRecognitionRequest,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
):
    job = await kiln_knowledge.queue_recognition(db, user.id, project_id, body)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.post("/projects/{project_id}/reparse")
async def reparse(project_id: str, db: DbSession, user: CurrentUser, background: BackgroundTasks):
    job = await service.reparse_project(db, user.id, project_id)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.post("/projects/{project_id}/datasets")
async def confirm(project_id: str, body: ConfirmRequest, db: DbSession, user: CurrentUser):
    return ok(await service.confirm_dataset(db, user.id, project_id, body))


@router.post("/projects/{project_id}/interpret")
async def interpret(project_id: str, db: DbSession, user: CurrentUser, background: BackgroundTasks):
    project = await service.get_asset(db, user.id, project_id, "project")
    if project.payload.get("status") != "parsed":
        raise AppError(ErrorCode.VALIDATION, "资料尚未解析完成", 422)
    job = await service.new_job(db, user.id, project_id, "interpret", {})
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.post("/projects/{project_id}/mechanisms/generate")
async def generate(
    project_id: str,
    body: MechanismRequest,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
):
    await service.get_asset(db, user.id, project_id, "project")
    dataset = await service.get_asset(db, user.id, body.datasetId, "dataset")
    if dataset.project_id != project_id:
        raise AppError(ErrorCode.VALIDATION, "数据集不属于当前项目", 422)
    job = await service.new_job(db, user.id, project_id, "mechanisms", body.model_dump())
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.post("/projects/{project_id}/mechanisms")
async def save(project_id: str, body: SaveMechanismRequest, db: DbSession, user: CurrentUser):
    return ok(await service.save_mechanism(db, user.id, project_id, body))


@router.get("/knowledge")
async def knowledge(db: DbSession, user: CurrentUser):
    return ok({"items": await service.list_assets(db, user.id, "mechanism")})


@router.get("/mechanism-catalog")
async def mechanism_catalog(
    db: DbSession, user: CurrentUser, dataset_id: str | None = Query(default=None, max_length=32)
):
    return ok(await service.mechanism_catalog(db, user.id, dataset_id))


@router.post("/mechanism-catalog/{base_id}/revisions")
async def revise_catalog(
    base_id: str, body: CatalogRevisionRequest, db: DbSession, user: CurrentUser
):
    return ok(await service.revise_catalog(db, user.id, base_id, body))


@router.post("/mechanism-catalog/history/{plan_id}/{key}")
async def retain_catalog_history(
    plan_id: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
    dataset_id: str | None = Query(default=None, max_length=32),
):
    return ok(await service.materialize_history_catalog(db, user.id, plan_id, key, dataset_id))


@router.get("/projects/{project_id}/mechanism-library")
async def mechanism_library(project_id: str, dataset_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.mechanism_library(db, user.id, project_id, dataset_id))


@router.post("/projects/{project_id}/mechanisms/compare")
async def compare_mechanisms(
    project_id: str,
    body: MechanismComparisonRequest,
    db: DbSession,
    user: CurrentUser,
):
    return ok(await service.validate_mechanism_comparison(db, user.id, project_id, body))


@router.post("/projects/{project_id}/experiments")
async def experiment(
    project_id: str,
    body: ExperimentRequest,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
):
    await service.get_asset(db, user.id, project_id, "project")
    dataset = await service.get_asset(db, user.id, body.datasetId, "dataset")
    mechanism = await service.get_asset(db, user.id, body.mechanismId, "mechanism")
    if (
        dataset.project_id != project_id
        or mechanism.project_id != project_id
        or mechanism.payload["datasetId"] != dataset.public_id
    ):
        raise AppError(ErrorCode.VALIDATION, "机理与数据版本不匹配", 422)
    definitions = await service.selected_algorithm_definitions(
        db, user.id, body.selectedAlgorithmKeys
    )
    from common.kiln_studio_engine import validate_algorithm_options

    try:
        body = validate_algorithm_options(body, definitions)
        fingerprints = {
            item.key: implementation_fingerprint(item)
            for item in definitions
            if item.key not in builtin_keys() and not item.baseKey and not item.external
        }
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, str(exc), 422) from exc
    job = await service.new_job(
        db,
        user.id,
        project_id,
        "experiment",
        body.model_dump(),
        algorithm_definitions=[item.model_dump(mode="json") for item in definitions],
        implementation_fingerprints=fingerprints,
    )
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.get("/algorithm-library")
async def algorithm_catalog(db: DbSession, user: CurrentUser):
    return ok({"items": await service.list_algorithm_catalog(db, user.id)})


@router.get("/algorithm-management")
async def algorithm_management_state(db: DbSession, admin: AdminUser):
    return ok(await algorithm_management.management_state(db, admin.id))


@router.post("/algorithm-management")
async def algorithm_draft(body: AlgorithmDraftRequest, db: DbSession, admin: AdminUser):
    return ok(await algorithm_management.create_draft(db, admin.id, body))


@router.post("/algorithm-management/packages")
async def algorithm_package_upload(
    request: Request, db: DbSession, admin: AdminUser,
    release_id: Annotated[str | None, Query(max_length=32)] = None,
):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
        raise AppError(ErrorCode.VALIDATION, "请上传 Python 算法 ZIP", 415)
    blob = bytearray()
    async for chunk in request.stream():
        if len(blob) + len(chunk) > MAX_PACKAGE:
            raise AppError(ErrorCode.VALIDATION, "算法 ZIP 大小上限为 1 MB", 413)
        blob.extend(chunk)
    return ok(await algorithm_management.upload_package(db, admin.id, bytes(blob), release_id))


@router.get("/algorithm-management/packages/example")
async def algorithm_package_example(admin: AdminUser):
    return Response(
        example_package(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="kiln-temperature-trend.zip"'},
    )


@router.post("/algorithm-management/{release_id}/bind")
async def algorithm_bind(
    release_id: str, body: AlgorithmBindRequest, db: DbSession, admin: AdminUser
):
    return ok(await algorithm_management.bind(db, admin.id, release_id, body))


@router.post("/algorithm-management/{release_id}/validate")
async def algorithm_validate(
    release_id: str,
    body: AlgorithmValidationRequest,
    db: DbSession,
    admin: AdminUser,
    background: BackgroundTasks,
):
    try:
        job = await algorithm_management.start_validation(db, admin.id, release_id, body)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, str(exc), 422) from exc
    background.add_task(service.execute_job, job["id"], admin.id)
    return ok(job)


@router.get("/algorithm-management/{release_id}/validation")
async def algorithm_validation_report(release_id: str, db: DbSession, admin: AdminUser):
    row = await service.get_asset(db, admin.id, release_id, algorithm_management.RELEASE)
    report_id = row.payload.get("validationId")
    report = (
        await service.get_asset(db, admin.id, report_id, algorithm_management.CHECK)
        if report_id
        else None
    )
    return ok({"report": service.asset_dict(report) if report else None})


@router.get("/algorithm-management/{release_id}/job")
async def algorithm_validation_job(release_id: str, db: DbSession, admin: AdminUser):
    row = await service.get_asset(db, admin.id, release_id, algorithm_management.RELEASE)
    job_id = row.payload.get("latestJobId")
    if not job_id:
        raise AppError(ErrorCode.NOT_FOUND, "当前版本尚未启动验证任务", 404)
    job = await service.get_asset(db, admin.id, job_id, "job")
    if job.payload["body"].get("releaseId") != release_id:
        raise AppError(ErrorCode.CONFLICT, "验证任务与算法记录不匹配", 409)
    return ok(service.asset_dict(job))


@router.post("/algorithm-management/{release_id}/publish")
async def algorithm_publish(
    release_id: str,
    body: AlgorithmPublishRequest,
    db: DbSession,
    admin: AdminUser,
):
    return ok(await algorithm_management.publish(db, admin.id, release_id, body))


@router.post("/algorithm-management/{release_id}/retire")
async def algorithm_retire(
    release_id: str,
    body: AlgorithmRetireRequest,
    db: DbSession,
    admin: AdminUser,
):
    return ok(await algorithm_management.retire(db, admin.id, release_id, body))


@router.post("/algorithm-library/presets")
async def create_algorithm_preset(body: AlgorithmPresetRequest, db: DbSession, user: CurrentUser):
    return ok(await service.save_algorithm_preset(db, user.id, body))


@router.get("/algorithm-library/external-contract")
async def external_contract(user: CurrentUser):
    return ok(
        {
            "features": FEATURES,
            "maxBytes": MAX_EXTERNAL_MODEL_BYTES,
            "formats": ["onnx"],
            "inputRank": 2,
            "inputCount": 1,
            "outputMode": "endpoint",
            "supportsSequence": False,
            "supportsMultipleHorizons": False,
            "requiresEmbeddedPreprocessing": True,
        }
    )


@router.post("/algorithm-library/external")
async def import_external(
    db: DbSession,
    user: CurrentUser,
    spec: str = Form(max_length=5000),
    file: UploadFile = File(...),  # noqa: B008
):
    try:
        body = ExternalAlgorithmImport.model_validate_json(spec)
    except ValidationError as exc:
        await file.close()
        detail = exc.errors(include_context=False, include_url=False, include_input=False)[0]
        field = ".".join(str(item) for item in detail["loc"])
        raise AppError(
            ErrorCode.VALIDATION,
            f"导入配置无效：{field + ' · ' if field else ''}{detail['msg']}",
            422,
        ) from exc
    return ok(await service.import_external_algorithm(db, user.id, file, body))


@router.post("/algorithm-library/{key}/availability")
async def algorithm_availability(
    key: str, body: AlgorithmAvailabilityRequest, db: DbSession, user: CurrentUser
):
    return ok(await service.set_algorithm_availability(db, user.id, key, body.enabled))


@router.post(
    "/projects/{project_id}/algorithm-recommendation",
    response_model=ApiResponse[AlgorithmRecommendationResponse],
)
async def algorithm_recommendation(
    project_id: str,
    body: AlgorithmRecommendationRequest,
    db: DbSession,
    user: CurrentUser,
    rules_only: bool = Query(False, alias="rulesOnly"),
):
    return ok(
        await service.recommend_algorithms(db, user.id, project_id, body, rules_only=rules_only)
    )


@router.post("/experiments/{experiment_id}/evaluation")
async def prediction_evaluation(
    experiment_id: str, body: PredictionEvaluationRequest, db: DbSession, user: CurrentUser
):
    return ok(await service.prediction_evaluation(db, user.id, experiment_id, body))


@router.get("/projects/{project_id}/files/{file_id}/preview")
async def table_preview(project_id: str, file_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.table_preview(db, user.id, project_id, file_id))


@router.get("/datasets/{dataset_id}/process-observations")
async def process_observations(
    dataset_id: str, db: DbSession, user: CurrentUser, cycle_id: str = Query(min_length=1)
):
    return ok(
        await service.temperature_observations(db, user.id, dataset_id, cycle_id, process=True)
    )


@router.get("/datasets/{dataset_id}/temperature-observations")
async def temperature_observations(
    dataset_id: str, db: DbSession, user: CurrentUser, cycle_id: str = Query(min_length=1)
):
    return ok(await service.temperature_observations(db, user.id, dataset_id, cycle_id))


@router.post("/projects/{project_id}/bundles")
async def bundle(project_id: str, body: BundleRequest, db: DbSession, user: CurrentUser):
    await service.get_asset(db, user.id, project_id, "project")
    return ok(await service.build_bundle(db, user.id, project_id, body))


@router.post("/projects/{project_id}/replays")
async def create_replay(
    project_id: str,
    body: ReplayRequest,
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
):
    job = await service.queue_replay(db, user.id, project_id, body)
    background.add_task(service.execute_job, job["id"], user.id)
    return ok(job)


@router.get("/replays/{replay_id}", response_model=ApiResponse[ReplayPackage])
async def replay(replay_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.get_replay(db, user.id, replay_id))


@router.post("/bundles/{bundle_id}/dashboard-layout")
async def dashboard_layout(
    bundle_id: str, body: DashboardLayoutRequest, db: DbSession, user: CurrentUser
):
    return ok(await service.generate_dashboard_layout(db, user.id, bundle_id, body))


@router.post("/bundles/{bundle_id}/simulate")
async def simulate(bundle_id: str, body: SimulateRequest, db: DbSession, user: CurrentUser):
    return ok(await service.simulate_bundle(db, user.id, bundle_id, body))


@router.get("/bundles/{bundle_id}/export")
async def export(
    bundle_id: str,
    db: DbSession,
    user: CurrentUser,
    format: Literal["json", "skill", "python", "onnx"] = Query(
        default="json", description="支持格式：json、skill、python、onnx"
    ),
):
    return ok(await service.export_bundle(db, user.id, bundle_id, format))


@router.get("/bundles/{bundle_id}/export/capabilities")
async def export_capabilities(
    bundle_id: Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[a-f0-9]+$")],
    db: DbSession,
    user: CurrentUser,
):
    return ok(await service.export_capabilities(db, user.id, bundle_id))


@router.get("/bundles/{bundle_id}/export/{package_format}.zip")
async def export_package_zip(
    bundle_id: Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[a-f0-9]+$")],
    package_format: Literal["python", "onnx"],
    db: DbSession,
    user: CurrentUser,
):
    from common.kiln_model_export import zip_python_files

    package = await service.export_bundle(db, user.id, bundle_id, package_format)
    # 版本来自真实 bundle；UTF-8 文件名可兼容中文，避免直接拼接响应头。
    filename = f"kiln-model-{package['version']}.zip"
    if package_format == "onnx":
        from common.kiln_onnx_export import zip_onnx_files

        content = zip_onnx_files(package)
        filename = f"kiln-model-{package['version']}.onnx.zip"
    else:
        content = zip_python_files(package)
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")},
    )


@router.post("/bundles/{bundle_id}/temperature-forecast")
async def temperature_forecast(
    bundle_id: str, body: TemperatureForecastRequest, db: DbSession, user: CurrentUser
):
    return ok(await service.forecast_temperature(db, user.id, bundle_id, body))


@router.get("/bundles/{bundle_id}/temperature-records")
async def temperature_records(bundle_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.list_temperature_records(db, user.id, bundle_id))


@router.post("/bundles/{bundle_id}/temperature-records/preview")
async def preview_temperature_record(
    bundle_id: str,
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),  # noqa: B008
):
    return ok(await service.upload_temperature_record(db, user.id, bundle_id, file))


@router.post("/bundles/{bundle_id}/temperature-records")
async def save_temperature_record(
    bundle_id: str,
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),  # noqa: B008
    settings: str = Form(..., max_length=30_000),
):
    try:
        body = ConfirmTemperatureRecord.model_validate_json(settings)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, "烧成记录映射格式无效，请重新确认", 422) from exc
    return ok(await service.upload_temperature_record(db, user.id, bundle_id, file, body))


@router.post("/temperature-runs/{run_id}/report")
async def temperature_report(run_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.temperature_report(db, user.id, run_id))


@router.get("/temperature-runs/{run_id}")
async def temperature_run(run_id: str, db: DbSession, user: CurrentUser):
    return ok(await service.get_temperature_run(db, user.id, run_id))


@router.get("/assets/{asset_id}")
async def asset(asset_id: str, db: DbSession, user: CurrentUser):
    return ok(service.asset_dict(await service.get_asset(db, user.id, asset_id)))


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: str, db: DbSession, user: CurrentUser, background: BackgroundTasks):
    job = await service.get_asset(db, user.id, job_id, "job")
    if job.payload["operation"] == "algorithm_validate":
        raise AppError(ErrorCode.VALIDATION, "请从算法管理页重新验证，以核对当前实现和权限", 422)
    if job.payload["status"] not in {"failed", "interrupted"}:
        raise AppError(ErrorCode.VALIDATION, "只有失败或中断任务可以重试", 422)
    if job.payload["operation"] in {"parse", "knowledge_recognition"}:
        await kiln_knowledge.editable_project(db, user.id, job.project_id)
    await service._job_update(
        db, job, status="queued", progress=0, error=None, message="已重新排队"
    )
    background.add_task(service.execute_job, job_id, user.id)
    return ok(service.asset_dict(job))
