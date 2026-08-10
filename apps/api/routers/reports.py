from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession
from api.services import biz_reports as reports_svc
from common.response import ok

router = APIRouter(prefix="/reports", tags=["reports"])


class GenerateBody(BaseModel):
    templateKey: str | None = None
    templateName: str | None = None
    type: str | None = Field(default=None, description="日报/周报/月报/年报/专项")
    period: str | None = None


@router.get("/templates")
async def report_templates(user: CurrentUser) -> dict:
    _ = user
    return ok({"items": reports_svc.list_templates()})


@router.get("")
async def reports_list(
    db: DbSession,
    user: CurrentUser,
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    _ = user
    data = await reports_svc.list_reports(
        db, report_type=type, status=status, limit=limit
    )
    return ok(data)


@router.post("/generate")
async def reports_generate(
    body: GenerateBody, db: DbSession, user: CurrentUser
) -> dict:
    item = await reports_svc.generate_report(
        db,
        user=user,
        template_key=body.templateKey,
        template_name=body.templateName,
        report_type=body.type,
        period=body.period,
    )
    return ok({"item": item})


@router.get("/{report_id}")
async def reports_get(report_id: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    item = await reports_svc.get_report_item(db, report_id)
    return ok({"item": item})


@router.get("/{report_id}/download")
async def reports_download(report_id: str, db: DbSession, user: CurrentUser) -> Response:
    _ = user
    row = await reports_svc.get_report(db, report_id)
    content = row.content or ""
    filename = f"{row.title}.md".replace("/", "-")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )
