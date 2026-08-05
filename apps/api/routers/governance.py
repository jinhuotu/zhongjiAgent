from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession
from api.services import governance as gov_svc
from common.response import ok

router = APIRouter(prefix="/governance", tags=["governance"])


class CreateTaskBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    owner: str | None = "管理员"
    sourceType: str = "excel"


class UpdateTaskBody(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    owner: str | None = None
    sourceType: str | None = None


class ExcelPreviewBody(BaseModel):
    fileName: str
    sheetName: str = "Sheet1"
    headers: list[Any]
    rows: list[list[Any]]
    rowCount: int | None = None
    importedAt: str | None = None
    contentBase64: str | None = None


@router.get("/tasks")
async def tasks_list(db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok({"items": await gov_svc.list_tasks(db)})


@router.post("/tasks")
async def tasks_create(body: CreateTaskBody, db: DbSession, user: CurrentUser) -> dict:
    item = await gov_svc.create_task(
        db,
        name=body.name,
        description=body.description,
        owner=body.owner or "管理员",
        source_type=body.sourceType or "excel",
        created_by=user.id,
    )
    return ok({"item": item})


@router.get("/tasks/{task_id}")
async def tasks_get(task_id: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    item = await gov_svc.get_task_dict(db, task_id)
    return ok({"item": item})


@router.put("/tasks/{task_id}")
async def tasks_update(task_id: str, body: UpdateTaskBody, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    item = await gov_svc.update_task(
        db,
        task_id,
        name=body.name,
        description=body.description,
        owner=body.owner,
        source_type=body.sourceType,
    )
    return ok({"item": item})


@router.delete("/tasks/{task_id}")
async def tasks_delete(task_id: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    await gov_svc.delete_task(db, task_id)
    return ok({"deleted": True})


@router.post("/tasks/{task_id}/excel")
async def tasks_excel(task_id: str, body: ExcelPreviewBody, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    item = await gov_svc.save_excel_preview(
        db,
        task_id,
        excel={
            "fileName": body.fileName,
            "sheetName": body.sheetName,
            "headers": body.headers,
            "rows": body.rows,
            "rowCount": body.rowCount,
            "importedAt": body.importedAt,
        },
        content_base64=body.contentBase64,
    )
    return ok({"item": item})


@router.get("/search")
async def tasks_search(db: DbSession, user: CurrentUser, q: str = "") -> dict:
    _ = user
    return ok({"items": await gov_svc.search_for_chat(db, q, top_k=5)})
