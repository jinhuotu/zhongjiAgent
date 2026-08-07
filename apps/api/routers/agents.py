from __future__ import annotations

from fastapi import APIRouter

from api.deps import AdminUser, CurrentUser, DbSession
from api.schemas.agents import CreateAgentRequest, UpdateAgentRequest
from api.services.agents import configs as configs_svc
from common.response import ok

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/options")
async def agents_options(db: DbSession, user: CurrentUser) -> dict:
    """任意登录用户：对话页可选启用智能体（含配置字段）。"""
    _ = user
    return ok({"items": await configs_svc.list_options(db)})


@router.get("")
async def agents_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok({"items": await configs_svc.list_configs(db)})


@router.get("/{agent_id}")
async def agents_get(agent_id: str, db: DbSession, user: CurrentUser) -> dict:
    """登录用户：按 id 取完整配置（打开对话 / 详情）。禁用项返回 422。"""
    _ = user
    return ok({"item": await configs_svc.get_enabled_bundle(db, agent_id)})


@router.post("")
async def agents_create(body: CreateAgentRequest, db: DbSession, admin: AdminUser) -> dict:
    item = await configs_svc.create_config(
        db,
        name=body.name,
        remark=body.remark,
        enabled=body.enabled,
        prompt_id=body.promptId,
        knowledge_base_ids=body.knowledgeBaseIds,
        mode=body.mode,
        mcp_tool_ids=body.mcpToolIds,
        tools_enabled=body.toolsEnabled,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.patch("/{agent_id}")
async def agents_update(
    agent_id: str,
    body: UpdateAgentRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    fields = body.model_dump(exclude_unset=True)
    kwargs: dict = {"public_id": agent_id}
    if "name" in fields:
        kwargs["name"] = fields["name"]
    if "remark" in fields:
        kwargs["remark"] = fields["remark"]
    if "enabled" in fields:
        kwargs["enabled"] = fields["enabled"]
    if "promptId" in fields:
        kwargs["prompt_id"] = fields["promptId"]
    if "knowledgeBaseIds" in fields:
        kwargs["knowledge_base_ids"] = fields["knowledgeBaseIds"]
    if "mode" in fields:
        kwargs["mode"] = fields["mode"]
    if "mcpToolIds" in fields:
        kwargs["mcp_tool_ids"] = fields["mcpToolIds"]
    if "toolsEnabled" in fields:
        kwargs["tools_enabled"] = fields["toolsEnabled"]
    item = await configs_svc.update_config(db, **kwargs)
    return ok({"item": item})


@router.delete("/{agent_id}")
async def agents_delete(agent_id: str, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await configs_svc.delete_config(db, public_id=agent_id)
    return ok({"deleted": True})
