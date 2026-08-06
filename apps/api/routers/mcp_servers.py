from __future__ import annotations

from fastapi import APIRouter

from api.deps import AdminUser, CurrentUser, DbSession
from api.schemas.mcp import CreateMcpServerRequest, UpdateMcpServerRequest, UpdateMcpToolRequest
from api.services.mcp import servers as mcp_svc
from common.response import ok

router = APIRouter(prefix="/mcp-servers", tags=["mcp"])


@router.get("")
async def mcp_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok({"items": await mcp_svc.list_servers(db)})


@router.get("/runtime")
async def mcp_runtime(db: DbSession, user: CurrentUser) -> dict:
    """登录用户可读：当前启用的 MCP 工具摘要（不含密钥）。"""
    _ = user
    tools = await mcp_svc.list_enabled_tools_for_chat(db)
    return ok(
        {
            "enabledToolCount": len(tools),
            "tools": [
                {
                    "serverId": t["serverId"],
                    "serverName": t["serverName"],
                    "name": t["name"],
                    "description": t["description"],
                }
                for t in tools
            ],
        }
    )


@router.post("")
async def mcp_create(body: CreateMcpServerRequest, db: DbSession, admin: AdminUser) -> dict:
    item = await mcp_svc.create_server(
        db,
        name=body.name,
        transport=body.transport,
        url=body.url,
        command=body.command,
        args=body.args,
        env=body.env,
        headers=body.headers,
        timeout_seconds=body.timeoutSeconds,
        remark=body.remark,
        enabled=body.enabled,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.patch("/{server_id}")
async def mcp_update(
    server_id: str,
    body: UpdateMcpServerRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    item = await mcp_svc.update_server(
        db,
        public_id=server_id,
        name=body.name,
        transport=body.transport,
        url=body.url,
        command=body.command,
        args=body.args,
        env=body.env,
        headers=body.headers,
        timeout_seconds=body.timeoutSeconds,
        remark=body.remark,
        enabled=body.enabled,
    )
    return ok({"item": item})


@router.delete("/{server_id}")
async def mcp_delete(server_id: str, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await mcp_svc.delete_server(db, public_id=server_id)
    return ok({"deleted": True})


@router.post("/{server_id}/health")
async def mcp_health(server_id: str, db: DbSession, admin: AdminUser) -> dict:
    from common.logging import get_logger

    log = get_logger("api.routers.mcp_servers")
    log.warning("mcp health hit server_id=%s admin=%s", server_id, admin.id)
    _ = admin
    return ok(await mcp_svc.health_check(db, public_id=server_id))


@router.post("/{server_id}/refresh-tools")
async def mcp_refresh_tools(server_id: str, db: DbSession, admin: AdminUser) -> dict:
    from common.logging import get_logger

    log = get_logger("api.routers.mcp_servers")
    log.warning("mcp refresh-tools hit server_id=%s admin=%s", server_id, admin.id)
    _ = admin
    item = await mcp_svc.refresh_tools(db, public_id=server_id)
    return ok({"item": item})


@router.patch("/{server_id}/tools/{tool_id}")
async def mcp_update_tool(
    server_id: str,
    tool_id: str,
    body: UpdateMcpToolRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    item = await mcp_svc.update_tool(
        db,
        server_public_id=server_id,
        tool_public_id=tool_id,
        enabled=body.enabled,
    )
    return ok({"item": item})
