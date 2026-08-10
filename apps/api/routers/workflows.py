from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from api.deps import AdminUser, CurrentUser, DbSession
from api.services.workflows import crud as crud_svc
from api.services.workflows import runner as runner_svc
from common.response import ok

router = APIRouter(prefix="/workflows", tags=["workflows"])


class CreateWorkflowRequest(BaseModel):
    name: str
    remark: str | None = None
    domain: str = "ai"
    enabled: bool = True


class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    remark: str | None = None
    domain: str | None = None
    enabled: bool | None = None


class SaveGraphRequest(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class PublishWorkflowRequest(BaseModel):
    changelog: str | None = None


class RunWorkflowRequest(BaseModel):
    input: Any = None
    useDraft: bool = False


@router.get("")
async def workflows_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok({"items": await crud_svc.list_workflows(db)})


@router.get("/options")
async def workflows_options(db: DbSession, user: CurrentUser) -> dict:
    """任意登录用户：可选已发布工作流（AI 报告等引用）。"""
    _ = user
    return ok({"items": await crud_svc.list_published_options(db)})


@router.post("")
async def workflows_create(
    body: CreateWorkflowRequest, db: DbSession, admin: AdminUser
) -> dict:
    item = await crud_svc.create_workflow(
        db,
        name=body.name,
        remark=body.remark,
        domain=body.domain,
        enabled=body.enabled,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.get("/runs/{run_id}")
async def workflows_run_get(run_id: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok({"item": await crud_svc.get_run(db, run_id)})


@router.get("/{workflow_id}")
async def workflows_get(workflow_id: str, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok({"item": await crud_svc.get_workflow(db, workflow_id)})


@router.patch("/{workflow_id}")
async def workflows_update(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    fields = body.model_dump(exclude_unset=True)
    kwargs: dict = {"public_id": workflow_id}
    if "name" in fields:
        kwargs["name"] = fields["name"]
    if "remark" in fields:
        kwargs["remark"] = fields["remark"]
    if "domain" in fields:
        kwargs["domain"] = fields["domain"]
    if "enabled" in fields:
        kwargs["enabled"] = fields["enabled"]
    item = await crud_svc.update_workflow(db, **kwargs)
    return ok({"item": item})


@router.delete("/{workflow_id}")
async def workflows_delete(
    workflow_id: str, db: DbSession, admin: AdminUser
) -> dict:
    _ = admin
    await crud_svc.delete_workflow(db, public_id=workflow_id)
    return ok({"deleted": True})


@router.put("/{workflow_id}/graph")
async def workflows_save_graph(
    workflow_id: str,
    body: SaveGraphRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    item = await crud_svc.save_graph(
        db,
        public_id=workflow_id,
        graph={"nodes": body.nodes, "edges": body.edges},
        created_by=admin.id,
    )
    return ok({"item": item})


@router.post("/{workflow_id}/publish")
async def workflows_publish(
    workflow_id: str,
    db: DbSession,
    admin: AdminUser,
    body: PublishWorkflowRequest | None = None,
) -> dict:
    item = await crud_svc.publish_workflow(
        db,
        public_id=workflow_id,
        changelog=body.changelog if body else None,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.get("/{workflow_id}/runs")
async def workflows_runs_list(
    workflow_id: str,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    _ = user
    return ok(
        {"items": await crud_svc.list_runs(db, workflow_public_id=workflow_id, limit=limit)}
    )


@router.post("/{workflow_id}/runs")
async def workflows_run(
    workflow_id: str,
    body: RunWorkflowRequest,
    db: DbSession,
    user: CurrentUser,
) -> EventSourceResponse:
    # 预校验（失败直接抛业务错误，避免进入空 SSE）
    wf = await crud_svc.get_by_public_id(db, workflow_id)
    await crud_svc.resolve_run_version(db, wf, use_draft=bool(body.useDraft))

    async def event_generator():  # noqa: ANN202
        async for ev in runner_svc.run_workflow(
            db,
            workflow_public_id=workflow_id,
            input_data=body.input,
            use_draft=bool(body.useDraft),
            created_by=user.id,
            trigger="trial",
        ):
            yield ev

    return EventSourceResponse(event_generator())
