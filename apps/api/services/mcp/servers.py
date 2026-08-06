from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.mcp.client import McpClient, McpToolDef
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from db.models.mcp import McpServer, McpTool

logger = get_logger(__name__)

Transport = Literal["streamable_http", "sse", "stdio"]


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private",
    "connection",
    "conn_str",
    "connstr",
    "credential",
    "authorization",
    "auth",
)


def _is_secret_key(key: str) -> bool:
    k = (key or "").strip().lower().replace("-", "_")
    return any(h in k for h in _SECRET_KEY_HINTS)


def _mask_value(value: str) -> str:
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * min(8, len(s) - 4)}{s[-2:]}"


def _display_secret_map(data: dict | None) -> dict[str, str]:
    """非密钥明文展示；密钥类仅脱敏。避免 TRANSPORT=stdio 被显示成 st*io。"""
    out: dict[str, str] = {}
    for k, v in (data or {}).items():
        key = str(k)
        s = str(v)
        out[key] = _mask_value(s) if _is_secret_key(key) else s
    return out


def _merge_secret_map(
    current: dict | None,
    incoming: dict[str, str] | None,
    *,
    replace_all: bool,
) -> dict[str, str] | None:
    """合并 env/headers。

    - 值为空或含 *：保留原值（编辑回填脱敏/留空不改）
    - replace_all=False：按 key 合并，未提交的 key 保留
    - replace_all=True 且 incoming 为空 dict：清空
    """
    if incoming is None:
        return _normalize_str_map(current if isinstance(current, dict) else None)
    if replace_all and len(incoming) == 0:
        return None

    base: dict[str, str] = {}
    if isinstance(current, dict):
        base = {str(k): str(v) for k, v in current.items()}

    if replace_all:
        cleaned: dict[str, str] = {}
        for k, v in incoming.items():
            sv = str(v)
            if not sv.strip() or "*" in sv:
                if k in base:
                    cleaned[str(k)] = base[k]
                continue
            cleaned[str(k)] = sv
        return _normalize_str_map(cleaned)

    for k, v in incoming.items():
        sv = str(v)
        if not sv.strip() or "*" in sv:
            continue
        base[str(k)] = sv
    return _normalize_str_map(base)


def tool_to_item(tool: McpTool) -> dict[str, Any]:
    return {
        "id": tool.public_id,
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.input_schema or {},
        "enabled": bool(tool.enabled),
        "updatedAt": int(tool.updated_at.timestamp() * 1000) if tool.updated_at else 0,
    }


def to_item(server: McpServer, *, include_secrets: bool = False) -> dict[str, Any]:
    env = server.env if isinstance(server.env, dict) else {}
    headers = server.headers if isinstance(server.headers, dict) else {}
    env_display = _display_secret_map(env)
    headers_display = _display_secret_map(headers)
    return {
        "id": server.public_id,
        "name": server.name,
        "transport": server.transport,
        "url": server.url,
        "command": server.command,
        "args": server.args if isinstance(server.args, list) else [],
        "envMasked": env_display,
        "headersMasked": headers_display,
        "envSecretKeys": [k for k in env if _is_secret_key(str(k))],
        "headersSecretKeys": [k for k in headers if _is_secret_key(str(k))],
        "env": env if include_secrets else None,
        "headers": headers if include_secrets else None,
        "timeoutSeconds": float(server.timeout_seconds or 60),
        "remark": server.remark,
        "enabled": bool(server.enabled),
        "lastError": server.last_error,
        "lastCheckedAt": (
            int(server.last_checked_at.timestamp() * 1000) if server.last_checked_at else None
        ),
        "toolsCachedAt": (
            int(server.tools_cached_at.timestamp() * 1000) if server.tools_cached_at else None
        ),
        "toolCount": len(server.tools or []),
        "tools": [tool_to_item(t) for t in (server.tools or [])],
        "createdAt": int(server.created_at.timestamp() * 1000) if server.created_at else 0,
        "updatedAt": int(server.updated_at.timestamp() * 1000) if server.updated_at else 0,
    }


def _build_client(
    server: McpServer,
    *,
    timeout_seconds: float | None = None,
) -> McpClient:
    """构建 MCP 客户端。

    对话/健康检查均使用配置的 timeoutSeconds（默认 60），不再强行抬到 300s，
    避免 list_tables 卡住时前端长时间转圈。
    """
    timeout = float(
        timeout_seconds if timeout_seconds is not None else (server.timeout_seconds or 60)
    )
    env: dict[str, str] = {}
    if isinstance(server.env, dict):
        env = {str(k): str(v) for k, v in server.env.items()}

    # mssql-mcp-server：MSSQL_CONNECTION_STRING 是「匿名默认连接」。
    # 模型常误传 connectionName="default"，会去 namedConnections 查找并失败。
    # 自动补 CONNECTION_DEFAULT，使 default 与默认连接串指向同一库。
    default_cs = (env.get("MSSQL_CONNECTION_STRING") or "").strip()
    if default_cs:
        env.setdefault("CONNECTION_DEFAULT", default_cs)
        # 兼容模型可能传的其它习惯名
        env.setdefault("CONNECTION_BESTMES", default_cs)
        env.setdefault("CONNECTION_BESTMESDB", default_cs)

    return McpClient(
        transport=server.transport,
        url=server.url,
        command=server.command,
        args=server.args if isinstance(server.args, list) else [],
        env=env,
        headers={str(k): str(v) for k, v in (server.headers or {}).items()}
        if isinstance(server.headers, dict)
        else {},
        timeout_seconds=timeout,
    )


async def list_servers(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(McpServer)
        .options(selectinload(McpServer.tools))
        .order_by(McpServer.updated_at.desc())
    )
    return [to_item(s) for s in result.scalars().unique().all()]


async def get_by_public_id(db: AsyncSession, public_id: str) -> McpServer:
    result = await db.execute(
        select(McpServer)
        .where(McpServer.public_id == public_id)
        .options(selectinload(McpServer.tools))
    )
    server = result.scalar_one_or_none()
    if server is None:
        raise AppError(ErrorCode.NOT_FOUND, "mcp server not found", status_code=404)
    return server


def _normalize_transport(transport: str) -> Transport:
    t = (transport or "").strip().lower()
    if t not in ("streamable_http", "sse", "stdio"):
        raise AppError(
            ErrorCode.VALIDATION,
            "transport must be streamable_http, sse or stdio",
            status_code=422,
        )
    return t  # type: ignore[return-value]


def _normalize_str_map(data: dict[str, str] | None) -> dict[str, str] | None:
    if data is None:
        return None
    out: dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip()
        if not key:
            continue
        out[key[:128]] = str(v)[:2048]
    return out


async def create_server(
    db: AsyncSession,
    *,
    name: str,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
    remark: str | None = None,
    enabled: bool = True,
    created_by: int | None = None,
) -> dict[str, Any]:
    t = _normalize_transport(transport)
    if not name.strip():
        raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
    if t in ("streamable_http", "sse") and not (url or "").strip():
        raise AppError(ErrorCode.VALIDATION, "url required for http/sse", status_code=422)
    if t == "stdio" and not (command or "").strip():
        raise AppError(ErrorCode.VALIDATION, "command required for stdio", status_code=422)

    server = McpServer(
        public_id=short_id(12),
        name=name.strip()[:128],
        transport=t,
        url=(url or "").strip()[:1024] or None,
        command=(command or "").strip()[:512] or None,
        args=[str(x)[:256] for x in (args or [])][:64],
        env=_normalize_str_map(env),
        headers=_normalize_str_map(headers),
        timeout_seconds=float(timeout_seconds or 60),
        remark=(remark or "").strip()[:512] or None,
        enabled=enabled,
        created_by=created_by,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    # reload tools relationship
    server = await get_by_public_id(db, server.public_id)
    return to_item(server)


async def update_server(
    db: AsyncSession,
    *,
    public_id: str,
    name: str | None = None,
    transport: str | None = None,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    remark: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    server = await get_by_public_id(db, public_id)
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
        server.name = name.strip()[:128]
    if transport is not None:
        server.transport = _normalize_transport(transport)
    if url is not None:
        server.url = url.strip()[:1024] or None
    if command is not None:
        server.command = command.strip()[:512] or None
    if args is not None:
        server.args = [str(x)[:256] for x in args][:64]
    if env is not None:
        # 合并更新：脱敏/空值保留原密钥；未出现的 key 也保留（避免编辑丢连接串）
        server.env = _merge_secret_map(server.env, env, replace_all=False)
    if headers is not None:
        server.headers = _merge_secret_map(server.headers, headers, replace_all=False)
    if timeout_seconds is not None:
        server.timeout_seconds = float(timeout_seconds)
    if remark is not None:
        server.remark = remark.strip()[:512] or None
    if enabled is not None:
        server.enabled = enabled

    t = server.transport
    if t in ("streamable_http", "sse") and not (server.url or "").strip():
        raise AppError(ErrorCode.VALIDATION, "url required for http/sse", status_code=422)
    if t == "stdio" and not (server.command or "").strip():
        raise AppError(ErrorCode.VALIDATION, "command required for stdio", status_code=422)

    await db.commit()
    server = await get_by_public_id(db, public_id)
    return to_item(server)


async def delete_server(db: AsyncSession, *, public_id: str) -> None:
    server = await get_by_public_id(db, public_id)
    await db.delete(server)
    await db.commit()


async def _replace_tools(
    db: AsyncSession,
    server: McpServer,
    tools: list[McpToolDef],
) -> None:
    existing = {t.name: t for t in (server.tools or [])}
    seen: set[str] = set()
    for td in tools:
        seen.add(td.name)
        if td.name in existing:
            row = existing[td.name]
            row.description = (td.description or "")[:4000] or None
            row.input_schema = td.input_schema or {}
        else:
            db.add(
                McpTool(
                    public_id=short_id(12),
                    server_id=server.id,
                    name=td.name[:128],
                    description=(td.description or "")[:4000] or None,
                    input_schema=td.input_schema or {},
                    enabled=True,
                )
            )
    for name, row in existing.items():
        if name not in seen:
            await db.delete(row)


async def refresh_tools(db: AsyncSession, *, public_id: str) -> dict[str, Any]:
    server = await get_by_public_id(db, public_id)
    env_keys = sorted((server.env or {}).keys()) if isinstance(server.env, dict) else []
    has_conn = any("connection" in k.lower() for k in env_keys)
    logger.warning(
        "mcp refresh start id=%s transport=%s command=%s args=%s envKeys=%s hasConnString=%s",
        public_id,
        server.transport,
        server.command,
        server.args,
        env_keys,
        has_conn,
    )
    if server.transport == "stdio" and not has_conn:
        logger.warning(
            "mcp refresh: env 中未见 CONNECTION 类变量，进程可能秒退或无法工作"
        )
    client = _build_client(server)
    try:
        tools = await client.list_tools()
        await _replace_tools(db, server, tools)
        now = datetime.now(timezone.utc)
        server.last_error = None
        server.last_checked_at = now
        server.tools_cached_at = now
        await db.commit()
        logger.warning("mcp refresh ok id=%s tools=%s", public_id, len(tools))
    except AppError as exc:
        logger.error("mcp refresh AppError id=%s: %s", public_id, exc.msg)
        server.last_error = exc.msg[:2000]
        server.last_checked_at = datetime.now(timezone.utc)
        await db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("mcp refresh failed id=%s", public_id)
        server.last_error = str(exc)[:2000]
        server.last_checked_at = datetime.now(timezone.utc)
        await db.commit()
        raise AppError(ErrorCode.INTERNAL, f"MCP refresh failed: {exc}", status_code=502) from exc

    server = await get_by_public_id(db, public_id)
    return to_item(server)


async def health_check(db: AsyncSession, *, public_id: str) -> dict[str, Any]:
    server = await get_by_public_id(db, public_id)
    env_keys = sorted((server.env or {}).keys()) if isinstance(server.env, dict) else []
    logger.warning(
        "mcp health start id=%s transport=%s command=%s envKeys=%s",
        public_id,
        server.transport,
        server.command,
        env_keys,
    )
    client = _build_client(server)
    try:
        full_tools = await client.list_tools()
        await _replace_tools(db, server, full_tools)
        now = datetime.now(timezone.utc)
        server.last_error = None
        server.last_checked_at = now
        server.tools_cached_at = now
        await db.commit()
        server = await get_by_public_id(db, public_id)
        probe = {
            "ok": True,
            "transport": server.transport,
            "toolCount": len(full_tools),
            "tools": [{"name": t.name, "description": t.description} for t in full_tools],
        }
        logger.warning("mcp health ok id=%s tools=%s", public_id, len(full_tools))
        return {"ok": True, "server": to_item(server), "probe": probe}
    except AppError as exc:
        logger.error("mcp health AppError id=%s: %s", public_id, exc.msg)
        server.last_error = exc.msg[:2000]
        server.last_checked_at = datetime.now(timezone.utc)
        await db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("mcp health failed id=%s", public_id)
        server.last_error = str(exc)[:2000]
        server.last_checked_at = datetime.now(timezone.utc)
        await db.commit()
        raise AppError(ErrorCode.INTERNAL, f"MCP health failed: {exc}", status_code=502) from exc


async def update_tool(
    db: AsyncSession,
    *,
    server_public_id: str,
    tool_public_id: str,
    enabled: bool | None = None,
) -> dict[str, Any]:
    server = await get_by_public_id(db, server_public_id)
    tool = next((t for t in (server.tools or []) if t.public_id == tool_public_id), None)
    if tool is None:
        raise AppError(ErrorCode.NOT_FOUND, "mcp tool not found", status_code=404)
    if enabled is not None:
        tool.enabled = enabled
    await db.commit()
    server = await get_by_public_id(db, server_public_id)
    return to_item(server)


async def list_enabled_tools_for_chat(db: AsyncSession) -> list[dict[str, Any]]:
    """对话侧：启用的 server + 启用的 tool。"""
    result = await db.execute(
        select(McpServer)
        .where(McpServer.enabled.is_(True))
        .options(selectinload(McpServer.tools))
        .order_by(McpServer.updated_at.desc())
    )
    out: list[dict[str, Any]] = []
    for server in result.scalars().unique().all():
        for tool in server.tools or []:
            if not tool.enabled:
                continue
            out.append(
                {
                    "serverId": server.public_id,
                    "serverName": server.name,
                    "toolId": tool.public_id,
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
                    "server": server,
                    "tool": tool,
                }
            )
    return out


def _sanitize_mssql_tool_args(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """修正模型常见误用：connectionName=default 在 mssql-mcp 中不是合法命名连接。"""
    args = dict(arguments or {})
    if "connectionName" not in args:
        return args
    raw = args.get("connectionName")
    name = str(raw or "").strip().lower()
    # 空 / default / 与默认库同义名 → 删除，改走 MSSQL_CONNECTION_STRING
    if name in {"", "default", "bestmes", "bestmesdb", "bestmesdb_jytongda", "mssql", "primary"}:
        args.pop("connectionName", None)
        logger.info(
            "mcp sanitize dropped connectionName=%r for tool=%s (use default CS)",
            raw,
            tool_name,
        )
    return args


async def call_tool_on_server(
    db: AsyncSession,
    *,
    server_public_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    pool: Any | None = None,
) -> dict[str, Any]:
    server = await get_by_public_id(db, server_public_id)
    if not server.enabled:
        raise AppError(ErrorCode.FORBIDDEN, "mcp server disabled", status_code=403)
    tool = next((t for t in (server.tools or []) if t.name == tool_name), None)
    if tool is not None and not tool.enabled:
        raise AppError(ErrorCode.FORBIDDEN, "mcp tool disabled", status_code=403)
    # 对话超时过长（如 180s）时 list_databases 会空等很久；单次工具调用封顶 45s 更快失败
    chat_timeout = min(float(server.timeout_seconds or 60), 45.0)
    client = _build_client(server, timeout_seconds=chat_timeout)
    safe_args = _sanitize_mssql_tool_args(tool_name, arguments or {})
    if pool is not None:
        result = await pool.call_tool(server, client, tool_name, safe_args)
    else:
        result = await client.call_tool(tool_name, safe_args)
    return {
        "content": result.content,
        "isError": result.is_error,
        "raw": result.raw,
    }
