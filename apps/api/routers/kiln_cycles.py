from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession
from api.services import kiln_cycles as kiln_cycles_svc
from api.services import kiln_reports as kiln_reports_svc
from common.errors import AppError, ErrorCode
from common.kiln_analysis import AnalysisRequest
from common.response import ok

router = APIRouter(prefix="/kiln-cycles", tags=["kiln-cycles"])


class CycleMetaBody(BaseModel):
    curveVersion: str = Field(default="", max_length=64)
    loadKg: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)


@router.get("")
async def kiln_cycles_list(db: DbSession, user: CurrentUser) -> dict:
    _ = user
    items = await kiln_cycles_svc.list_cycles(db)
    return ok({"items": items})


@router.post("/upload")
async def kiln_cycles_upload(
    db: DbSession,
    user: CurrentUser,
    files: list[UploadFile] = File(...),  # noqa: B008
    kilnCode: str = Form(default=""),  # noqa: B008
    mappingsJson: str = Form(default="{}", max_length=100000),  # noqa: B008
) -> dict:
    _ = user
    try:
        mappings = json.loads(mappingsJson)
        if not isinstance(mappings, dict) or any(
            not isinstance(name, str)
            or not isinstance(fields, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in fields.items())
            for name, fields in mappings.items()
        ):
            raise ValueError("invalid mappings")
    except (ValueError, TypeError) as exc:
        raise AppError(ErrorCode.VALIDATION, "字段映射格式无效", 422) from exc
    data = await kiln_cycles_svc.upload_cycles(db, files, kilnCode.strip() or None, mappings)
    return ok(data)


@router.post("/preview")
async def kiln_cycles_preview(
    user: CurrentUser,
    files: list[UploadFile] = File(...),  # noqa: B008
    kilnCode: str = Form(default=""),  # noqa: B008
) -> dict:
    return ok({"files": await kiln_cycles_svc.preview_cycles(files, kilnCode.strip() or None)})


@router.patch("/{cycle_id}")
async def kiln_cycles_patch(
    cycle_id: int,
    body: CycleMetaBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    _ = user
    item = await kiln_cycles_svc.update_cycle_meta(
        db,
        cycle_id,
        curve_version=body.curveVersion,
        load_kg=body.loadKg,
    )
    return ok({"item": item})


@router.get("/{cycle_id}")
async def kiln_cycles_workspace(cycle_id: int, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    data = await kiln_cycles_svc.cycle_workspace(db, cycle_id)
    return ok(data)


@router.post("/{cycle_id}/analyze")
async def kiln_cycle_analyze(
    cycle_id: int, body: AnalysisRequest, db: DbSession, user: CurrentUser
) -> dict:
    return ok(await kiln_reports_svc.analyze_cycle(db, cycle_id, body))


@router.post("/{cycle_id}/report")
async def kiln_cycle_report(
    cycle_id: int, body: AnalysisRequest, db: DbSession, user: CurrentUser
) -> dict:
    return ok(await kiln_reports_svc.generate_report(db, cycle_id, user.id, body))


@router.get("/{cycle_id}/runs")
async def kiln_cycle_runs(cycle_id: int, db: DbSession, user: CurrentUser) -> dict:
    return ok({"items": await kiln_reports_svc.list_runs(db, cycle_id, user.id)})


@router.get("/{cycle_id}/runs/{run_id}")
async def kiln_cycle_run(cycle_id: int, run_id: str, db: DbSession, user: CurrentUser) -> dict:
    return ok(await kiln_reports_svc.get_run(db, cycle_id, run_id, user.id))
