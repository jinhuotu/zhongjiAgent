"""操作日志 / 登录日志：写入、查询、按保留天数滚动删除。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from common.config import get_settings
from common.logging import get_logger
from db.models.audit import LoginLog, OperationLog
from db.session import AsyncSessionLocal

logger = get_logger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 前缀长的放前面，避免误匹配
_PREFIX_MODULE: tuple[tuple[str, str], ...] = (
    ("/api/v1/hot-configs", "hot_configs"),
    ("/api/v1/mcp-servers", "mcp"),
    ("/api/v1/models", "models"),
    ("/api/v1/users", "users"),
    ("/api/v1/roles", "roles"),
)

_SKIP_RESOURCE_TOKENS = frozenset(
    {"reset-password", "refresh-tools", "health", "tools", "runtime"}
)

_ACTION_BY_TAIL = {
    "reset-password": "reset_password",
    "refresh-tools": "refresh_tools",
    "health": "health",
}

_METHOD_ACTION = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def retention_days() -> int:
    days = int(get_settings().audit_log_retention_days or 7)
    return max(1, days)


def retention_cutoff(now: datetime | None = None) -> datetime:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts - timedelta(days=retention_days())


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real[:64]
    if request.client and request.client.host:
        return str(request.client.host)[:64]
    return ""


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:512]


def match_write_target(method: str, path: str) -> tuple[str, str, str | None] | None:
    """若该请求应记操作日志，返回 (module, action, resource_id)。"""
    method_u = (method or "").upper()
    if method_u not in WRITE_METHODS:
        return None
    raw = (path or "").split("?", 1)[0]
    if raw != "/" and raw.endswith("/"):
        raw = raw.rstrip("/")
    for prefix, module in _PREFIX_MODULE:
        if raw == prefix or raw.startswith(prefix + "/"):
            rest = raw[len(prefix) :].lstrip("/")
            parts = [p for p in rest.split("/") if p]
            return module, _action_from(method_u, parts), _resource_id(parts)
    return None


def _action_from(method: str, parts: list[str]) -> str:
    if parts:
        tail = parts[-1]
        if tail in _ACTION_BY_TAIL:
            return _ACTION_BY_TAIL[tail]
        if "tools" in parts:
            return "update_tool"
    return _METHOD_ACTION.get(method, method.lower())


def _resource_id(parts: list[str]) -> str | None:
    ids = [p for p in parts if p not in _SKIP_RESOURCE_TOKENS]
    if not ids:
        return None
    joined = "/".join(ids)
    return joined[:128]


def _ts_ms(dt: datetime | None) -> int:
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def error_msg_from_body(body: bytes | None, status_code: int) -> str | None:
    if status_code < 400 or not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    msg = data.get("msg")
    if msg is None:
        return None
    text = str(msg).strip()
    return text[:512] if text else None


async def purge_expired_logs(db: AsyncSession | None = None) -> dict[str, int]:
    """删除早于保留窗口的操作/登录日志。"""
    cutoff = retention_cutoff()

    async def _run(session: AsyncSession) -> dict[str, int]:
        op = await session.execute(
            delete(OperationLog).where(OperationLog.created_at < cutoff)
        )
        lg = await session.execute(delete(LoginLog).where(LoginLog.created_at < cutoff))
        await session.commit()
        return {
            "operations": int(op.rowcount or 0),
            "logins": int(lg.rowcount or 0),
        }

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


async def record_login(
    *,
    username: str,
    success: bool,
    reason: str,
    ip: str,
    user_agent_str: str,
    user_id: int | None = None,
) -> None:
    name = (username or "").strip()[:64] or "-"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                LoginLog(
                    username=name,
                    user_id=user_id,
                    success=bool(success),
                    reason=(reason or "")[:64],
                    ip=(ip or "")[:64],
                    user_agent=(user_agent_str or "")[:512],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("record login log failed username=%s success=%s", name, success)
        return
    await _purge_quietly()


async def record_operation(
    *,
    module: str,
    action: str,
    method: str,
    path: str,
    resource_id: str | None,
    operator_username: str | None,
    success: bool,
    status_code: int,
    ip: str,
    user_agent_str: str,
    detail: str | None = None,
    error_msg: str | None = None,
    duration_ms: int = 0,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                OperationLog(
                    module=(module or "")[:32],
                    action=(action or "")[:32],
                    method=(method or "")[:16],
                    path=(path or "")[:512],
                    resource_id=(resource_id[:128] if resource_id else None),
                    operator_username=(
                        operator_username[:64] if operator_username else None
                    ),
                    success=bool(success),
                    status_code=int(status_code or 0),
                    ip=(ip or "")[:64],
                    user_agent=(user_agent_str or "")[:512],
                    detail=(detail[:512] if detail else None),
                    error_msg=(error_msg[:512] if error_msg else None),
                    duration_ms=max(0, int(duration_ms or 0)),
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("record operation log failed path=%s", path)
        return
    await _purge_quietly()


async def _purge_quietly() -> None:
    try:
        await purge_expired_logs()
    except Exception:  # noqa: BLE001
        logger.exception("audit log purge after write failed")


def to_login_item(row: LoginLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "userId": row.user_id,
        "success": bool(row.success),
        "reason": row.reason,
        "ip": row.ip,
        "userAgent": row.user_agent,
        "createdAt": _ts_ms(row.created_at),
    }


def to_operation_item(row: OperationLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "module": row.module,
        "action": row.action,
        "method": row.method,
        "path": row.path,
        "resourceId": row.resource_id,
        "operatorId": row.operator_id,
        "operatorUsername": row.operator_username,
        "success": bool(row.success),
        "statusCode": row.status_code,
        "ip": row.ip,
        "userAgent": row.user_agent,
        "detail": row.detail,
        "errorMsg": row.error_msg,
        "durationMs": row.duration_ms,
        "createdAt": _ts_ms(row.created_at),
    }


async def list_login_logs(
    db: AsyncSession,
    *,
    success: bool | None = None,
    username: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    cutoff = retention_cutoff()
    stmt = select(LoginLog).where(LoginLog.created_at >= cutoff)
    count_stmt = select(func.count()).select_from(LoginLog).where(
        LoginLog.created_at >= cutoff
    )
    if success is not None:
        stmt = stmt.where(LoginLog.success.is_(success))
        count_stmt = count_stmt.where(LoginLog.success.is_(success))
    name = (username or "").strip()
    if name:
        like = f"%{name}%"
        stmt = stmt.where(LoginLog.username.like(like))
        count_stmt = count_stmt.where(LoginLog.username.like(like))

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(LoginLog.id.desc()).offset(offset).limit(limit)
    )
    items = [to_login_item(r) for r in result.scalars().all()]
    return {
        "items": items,
        "total": total,
        "retentionDays": retention_days(),
        "cutoffAt": _ts_ms(cutoff),
    }


async def list_operation_logs(
    db: AsyncSession,
    *,
    module: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    cutoff = retention_cutoff()
    stmt = select(OperationLog).where(OperationLog.created_at >= cutoff)
    count_stmt = select(func.count()).select_from(OperationLog).where(
        OperationLog.created_at >= cutoff
    )
    mod = (module or "").strip()
    if mod:
        stmt = stmt.where(OperationLog.module == mod)
        count_stmt = count_stmt.where(OperationLog.module == mod)
    if success is not None:
        stmt = stmt.where(OperationLog.success.is_(success))
        count_stmt = count_stmt.where(OperationLog.success.is_(success))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        cond = or_(
            OperationLog.path.like(like),
            OperationLog.operator_username.like(like),
            OperationLog.resource_id.like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(OperationLog.id.desc()).offset(offset).limit(limit)
    )
    items = [to_operation_item(r) for r in result.scalars().all()]
    return {
        "items": items,
        "total": total,
        "retentionDays": retention_days(),
        "cutoffAt": _ts_ms(cutoff),
    }


async def summary(db: AsyncSession) -> dict[str, Any]:
    cutoff = retention_cutoff()
    op_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.created_at >= cutoff)
            )
        ).scalar_one()
        or 0
    )
    op_fail = int(
        (
            await db.execute(
                select(func.count())
                .select_from(OperationLog)
                .where(
                    OperationLog.created_at >= cutoff,
                    OperationLog.success.is_(False),
                )
            )
        ).scalar_one()
        or 0
    )
    login_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(LoginLog)
                .where(LoginLog.created_at >= cutoff)
            )
        ).scalar_one()
        or 0
    )
    login_fail = int(
        (
            await db.execute(
                select(func.count())
                .select_from(LoginLog)
                .where(LoginLog.created_at >= cutoff, LoginLog.success.is_(False))
            )
        ).scalar_one()
        or 0
    )
    return {
        "retentionDays": retention_days(),
        "cutoffAt": _ts_ms(cutoff),
        "operationTotal": op_total,
        "operationFail": op_fail,
        "loginTotal": login_total,
        "loginFail": login_fail,
    }
