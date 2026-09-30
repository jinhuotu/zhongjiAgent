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
from db.models.audit import DataQueryLog, KnowledgeAudit, LoginLog, OperationLog
from db.models.chat import ChatMessage, ChatSession
from db.models.hot_config import HotConfigAudit
from db.models.user import User
from db.session import AsyncSessionLocal

logger = get_logger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 前缀长的放前面，避免误匹配
_PREFIX_MODULE: tuple[tuple[str, str], ...] = (
    ("/api/v1/hot-configs", "hot_configs"),
    ("/api/v1/mcp-servers", "mcp"),
    ("/api/v1/knowledge", "knowledge"),
    ("/api/v1/workflows", "workflows"),
    ("/api/v1/governance", "governance"),
    ("/api/v1/prompts", "prompts"),
    ("/api/v1/agents", "agents"),
    ("/api/v1/models", "models"),
    ("/api/v1/users", "users"),
    ("/api/v1/roles", "roles"),
)

# 写入方法但属于查询，不进操作日志（另记 data_query_logs）
_SKIP_WRITE_PATHS = frozenset({"/api/v1/knowledge/search"})

_SKIP_RESOURCE_TOKENS = frozenset(
    {
        "reset-password",
        "refresh-tools",
        "health",
        "tools",
        "runtime",
        "publish",
        "runs",
        "graph",
        "excel",
    }
)

_ACTION_BY_TAIL = {
    "reset-password": "reset_password",
    "refresh-tools": "refresh_tools",
    "health": "health",
    "publish": "publish",
    "runs": "run",
    "graph": "update_graph",
    "excel": "export_excel",
}

_METHOD_ACTION = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def retention_days() -> int:
    """操作 / 登录日志保留天数（运维窗口）。"""
    days = int(get_settings().audit_log_retention_days or 7)
    return max(1, days)


def trace_retention_days() -> int:
    """知识库变更 / 数据查询 / 已软删问答的合规保留天数。"""
    days = int(get_settings().audit_trace_retention_days or 180)
    return max(retention_days(), days)


def export_max_rows() -> int:
    rows = int(get_settings().audit_export_max_rows or 5000)
    return max(100, min(rows, 20000))


def retention_cutoff(now: datetime | None = None) -> datetime:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts - timedelta(days=retention_days())


def trace_retention_cutoff(now: datetime | None = None) -> datetime:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts - timedelta(days=trace_retention_days())


def request_id_of(request: Request) -> str:
    rid = str(getattr(request.state, "request_id", "") or "").strip()
    if rid:
        return rid[:64]
    header = (request.headers.get("x-request-id") or "").strip()
    return header[:64] if header else ""


def build_operation_detail(
    *,
    module: str,
    action: str,
    resource_id: str | None,
    status_code: int,
) -> str:
    parts = [f"{module}.{action}"]
    if resource_id:
        parts.append(f"resource={resource_id}")
    parts.append(f"status={status_code}")
    text = " ".join(parts)
    return text[:512] if len(text) <= 512 else text[:511] + "…"


async def resolve_operator_id(username: str | None) -> int | None:
    name = (username or "").strip()
    if not name:
        return None
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User.id).where(User.username == name))
            uid = result.scalar_one_or_none()
            return int(uid) if uid is not None else None
    except Exception:  # noqa: BLE001
        logger.exception("resolve operator id failed username=%s", name)
        return None


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
    if raw in _SKIP_WRITE_PATHS:
        return None
    # 问答正文不进操作日志；仅会话删除留痕（正文走问答审计，删除改为软删）
    if method_u == "DELETE" and raw.startswith("/api/v1/ai/sessions/"):
        rest = raw[len("/api/v1/ai/sessions/") :]
        if rest and "/" not in rest:
            return "ai", "delete_session", rest[:128]
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
    """按保留策略清理：运维窗口清理操作/登录；合规窗口清理知识库变更/查询/软删问答。"""
    ops_cutoff = retention_cutoff()
    trace_cutoff = trace_retention_cutoff()

    async def _run(session: AsyncSession) -> dict[str, int]:
        op = await session.execute(
            delete(OperationLog).where(OperationLog.created_at < ops_cutoff)
        )
        lg = await session.execute(delete(LoginLog).where(LoginLog.created_at < ops_cutoff))
        kb = await session.execute(
            delete(KnowledgeAudit).where(KnowledgeAudit.created_at < trace_cutoff)
        )
        dq = await session.execute(
            delete(DataQueryLog).where(DataQueryLog.created_at < trace_cutoff)
        )
        hc = await session.execute(
            delete(HotConfigAudit).where(HotConfigAudit.created_at < trace_cutoff)
        )
        # 软删足够久的问答会话（级联删消息）
        chats = await session.execute(
            delete(ChatSession).where(
                ChatSession.deleted_at.is_not(None),
                ChatSession.deleted_at < trace_cutoff,
            )
        )
        await session.commit()
        return {
            "operations": int(op.rowcount or 0),
            "logins": int(lg.rowcount or 0),
            "knowledge": int(kb.rowcount or 0),
            "queries": int(dq.rowcount or 0),
            "hotConfigs": int(hc.rowcount or 0),
            "chats": int(chats.rowcount or 0),
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
    operator_id: int | None = None,
    request_id: str = "",
) -> None:
    if operator_id is None and operator_username:
        operator_id = await resolve_operator_id(operator_username)
    if not detail:
        detail = build_operation_detail(
            module=module,
            action=action,
            resource_id=resource_id,
            status_code=status_code,
        )
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                OperationLog(
                    module=(module or "")[:32],
                    action=(action or "")[:32],
                    method=(method or "")[:16],
                    path=(path or "")[:512],
                    resource_id=(resource_id[:128] if resource_id else None),
                    operator_id=operator_id,
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
                    request_id=(request_id or "")[:64],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("record operation log failed path=%s", path)
        return


def _purge_quietly() -> None:
    """兼容旧调用；写路径不再同步 purge，改由 Worker / 启动时清理。"""
    return


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
        "requestId": getattr(row, "request_id", "") or "",
        "createdAt": _ts_ms(row.created_at),
    }


async def list_login_logs(
    db: AsyncSession,
    *,
    success: bool | None = None,
    username: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
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
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(LoginLog.created_at >= start)
        count_stmt = count_stmt.where(LoginLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(LoginLog.created_at <= end)
        count_stmt = count_stmt.where(LoginLog.created_at <= end)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(LoginLog.id.desc()).offset(offset).limit(limit)
    )
    items = [to_login_item(r) for r in result.scalars().all()]
    return {
        "items": items,
        "total": total,
        "retentionDays": retention_days(),
        "traceRetentionDays": trace_retention_days(),
        "cutoffAt": _ts_ms(cutoff),
    }


async def list_operation_logs(
    db: AsyncSession,
    *,
    module: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    username: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
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
    uname = (username or "").strip()
    if uname:
        like = f"%{uname}%"
        stmt = stmt.where(OperationLog.operator_username.like(like))
        count_stmt = count_stmt.where(OperationLog.operator_username.like(like))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        cond = or_(
            OperationLog.path.like(like),
            OperationLog.operator_username.like(like),
            OperationLog.resource_id.like(like),
            OperationLog.detail.like(like),
            OperationLog.request_id.like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(OperationLog.created_at >= start)
        count_stmt = count_stmt.where(OperationLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(OperationLog.created_at <= end)
        count_stmt = count_stmt.where(OperationLog.created_at <= end)

    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(OperationLog.id.desc()).offset(offset).limit(limit)
    )
    items = [to_operation_item(r) for r in result.scalars().all()]
    return {
        "items": items,
        "total": total,
        "retentionDays": retention_days(),
        "traceRetentionDays": trace_retention_days(),
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
        "traceRetentionDays": trace_retention_days(),
        "cutoffAt": _ts_ms(cutoff),
        "traceCutoffAt": _ts_ms(trace_retention_cutoff()),
        "operationTotal": op_total,
        "operationFail": op_fail,
        "loginTotal": login_total,
        "loginFail": login_fail,
        "knowledgeTotal": int(
            (await db.execute(select(func.count()).select_from(KnowledgeAudit))).scalar_one()
            or 0
        ),
        "queryTotal": int(
            (await db.execute(select(func.count()).select_from(DataQueryLog))).scalar_one() or 0
        ),
        "chatSessionTotal": int(
            (await db.execute(select(func.count()).select_from(ChatSession))).scalar_one() or 0
        ),
        "hotConfigTotal": int(
            (await db.execute(select(func.count()).select_from(HotConfigAudit))).scalar_one()
            or 0
        ),
    }


_JSON_LIMIT = 4000
_TEXT_LIMIT = 512


def dumps_limited(value: Any, *, limit: int = _JSON_LIMIT) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def clip(text: str | None, limit: int = _TEXT_LIMIT) -> str:
    raw = (text or "").replace("\n", " ").strip()
    if len(raw) > limit:
        return raw[: limit - 1] + "…"
    return raw


def _ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


async def record_knowledge_change(
    *,
    action: str,
    base_id: str | None,
    doc_id: str | None = None,
    operator_id: int | None = None,
    operator_username: str | None = None,
    before: Any = None,
    after: Any = None,
    ip: str = "",
    request_id: str = "",
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                KnowledgeAudit(
                    action=(action or "")[:32],
                    base_id=(base_id[:32] if base_id else None),
                    doc_id=(doc_id[:32] if doc_id else None),
                    operator_id=operator_id,
                    operator_username=(
                        operator_username[:64] if operator_username else None
                    ),
                    before_json=dumps_limited(before),
                    after_json=dumps_limited(after),
                    ip=(ip or "")[:64],
                    request_id=(request_id or "")[:64],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("record knowledge audit failed action=%s base=%s", action, base_id)


async def record_data_query(
    *,
    source: str,
    query_text: str,
    success: bool,
    duration_ms: int = 0,
    tool_name: str | None = None,
    params: Any = None,
    result_summary: str | None = None,
    operator_id: int | None = None,
    operator_username: str | None = None,
    session_id: str | None = None,
    ip: str = "",
    request_id: str = "",
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                DataQueryLog(
                    source=(source or "")[:32],
                    query_text=clip(query_text),
                    tool_name=(tool_name[:128] if tool_name else None),
                    params_json=dumps_limited(params),
                    result_summary=clip(result_summary) or None,
                    success=bool(success),
                    duration_ms=max(0, int(duration_ms or 0)),
                    operator_id=operator_id,
                    operator_username=(
                        operator_username[:64] if operator_username else None
                    ),
                    session_id=(session_id[:32] if session_id else None),
                    ip=(ip or "")[:64],
                    request_id=(request_id or "")[:64],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("record data query log failed source=%s", source)


def to_knowledge_item(row: KnowledgeAudit) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "baseId": row.base_id,
        "docId": row.doc_id,
        "operatorId": row.operator_id,
        "operatorUsername": row.operator_username,
        "beforeJson": row.before_json,
        "afterJson": row.after_json,
        "ip": row.ip,
        "requestId": row.request_id,
        "createdAt": _ts_ms(row.created_at),
    }


def to_query_item(row: DataQueryLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "queryText": row.query_text,
        "toolName": row.tool_name,
        "paramsJson": row.params_json,
        "resultSummary": row.result_summary,
        "success": bool(row.success),
        "durationMs": row.duration_ms,
        "operatorId": row.operator_id,
        "operatorUsername": row.operator_username,
        "sessionId": row.session_id,
        "ip": row.ip,
        "requestId": row.request_id,
        "createdAt": _ts_ms(row.created_at),
    }


async def list_knowledge_audits(
    db: AsyncSession,
    *,
    action: str | None = None,
    keyword: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(KnowledgeAudit)
    count_stmt = select(func.count()).select_from(KnowledgeAudit)
    act = (action or "").strip()
    if act:
        stmt = stmt.where(KnowledgeAudit.action == act)
        count_stmt = count_stmt.where(KnowledgeAudit.action == act)
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        cond = or_(
            KnowledgeAudit.base_id.like(like),
            KnowledgeAudit.doc_id.like(like),
            KnowledgeAudit.operator_username.like(like),
            KnowledgeAudit.before_json.like(like),
            KnowledgeAudit.after_json.like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(KnowledgeAudit.created_at >= start)
        count_stmt = count_stmt.where(KnowledgeAudit.created_at >= start)
    if end is not None:
        stmt = stmt.where(KnowledgeAudit.created_at <= end)
        count_stmt = count_stmt.where(KnowledgeAudit.created_at <= end)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(KnowledgeAudit.id.desc()).offset(offset).limit(limit)
    )
    return {
        "items": [to_knowledge_item(r) for r in result.scalars().all()],
        "total": total,
    }


async def list_data_queries(
    db: AsyncSession,
    *,
    source: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(DataQueryLog)
    count_stmt = select(func.count()).select_from(DataQueryLog)
    src = (source or "").strip()
    if src:
        stmt = stmt.where(DataQueryLog.source == src)
        count_stmt = count_stmt.where(DataQueryLog.source == src)
    if success is not None:
        stmt = stmt.where(DataQueryLog.success.is_(success))
        count_stmt = count_stmt.where(DataQueryLog.success.is_(success))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        cond = or_(
            DataQueryLog.query_text.like(like),
            DataQueryLog.tool_name.like(like),
            DataQueryLog.operator_username.like(like),
            DataQueryLog.session_id.like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(DataQueryLog.created_at >= start)
        count_stmt = count_stmt.where(DataQueryLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(DataQueryLog.created_at <= end)
        count_stmt = count_stmt.where(DataQueryLog.created_at <= end)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(DataQueryLog.id.desc()).offset(offset).limit(limit)
    )
    return {
        "items": [to_query_item(r) for r in result.scalars().all()],
        "total": total,
    }


def _clip_content(text: str | None, limit: int = 8000) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


async def list_chat_audits(
    db: AsyncSession,
    *,
    username: str | None = None,
    keyword: str | None = None,
    session_id: str | None = None,
    include_deleted: bool = True,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(ChatSession, User.username).join(User, User.id == ChatSession.user_id)
    count_stmt = (
        select(func.count())
        .select_from(ChatSession)
        .join(User, User.id == ChatSession.user_id)
    )
    if not include_deleted:
        stmt = stmt.where(ChatSession.deleted_at.is_(None))
        count_stmt = count_stmt.where(ChatSession.deleted_at.is_(None))
    sid = (session_id or "").strip()
    if sid:
        stmt = stmt.where(ChatSession.public_id == sid)
        count_stmt = count_stmt.where(ChatSession.public_id == sid)
    name = (username or "").strip()
    if name:
        like = f"%{name}%"
        stmt = stmt.where(User.username.like(like))
        count_stmt = count_stmt.where(User.username.like(like))
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        msg_hit = (
            select(ChatMessage.id)
            .where(
                ChatMessage.session_id == ChatSession.id,
                ChatMessage.content.like(like),
            )
            .exists()
        )
        cond = or_(ChatSession.title.like(like), msg_hit)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(ChatSession.updated_at >= start)
        count_stmt = count_stmt.where(ChatSession.updated_at >= start)
    if end is not None:
        stmt = stmt.where(ChatSession.updated_at <= end)
        count_stmt = count_stmt.where(ChatSession.updated_at <= end)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(ChatSession.updated_at.desc()).offset(offset).limit(limit)
    )
    items = []
    for session, uname in result.all():
        items.append(
            {
                "id": session.public_id,
                "title": session.title,
                "username": uname,
                "userId": session.user_id,
                "mode": session.mode,
                "messageCount": session.message_count,
                "deleted": session.deleted_at is not None,
                "deletedAt": _ts_ms(session.deleted_at),
                "createdAt": _ts_ms(session.created_at),
                "updatedAt": _ts_ms(session.updated_at),
            }
        )
    return {"items": items, "total": total}


async def get_chat_audit(db: AsyncSession, *, session_id: str) -> dict[str, Any]:
    result = await db.execute(
        select(ChatSession, User.username)
        .join(User, User.id == ChatSession.user_id)
        .where(ChatSession.public_id == session_id)
    )
    row = result.first()
    if row is None:
        return {}
    session, uname = row
    msgs = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.id.asc())
    )
    messages = []
    for m in msgs.scalars().all():
        messages.append(
            {
                "id": m.public_id,
                "role": m.role,
                "content": _clip_content(m.content),
                "toolName": m.tool_name,
                "createdAt": _ts_ms(m.created_at),
            }
        )
    related = await db.execute(
        select(DataQueryLog)
        .where(DataQueryLog.session_id == session.public_id)
        .order_by(DataQueryLog.id.desc())
        .limit(50)
    )
    related_queries = [to_query_item(r) for r in related.scalars().all()]
    return {
        "item": {
            "id": session.public_id,
            "title": session.title,
            "username": uname,
            "userId": session.user_id,
            "mode": session.mode,
            "messageCount": session.message_count,
            "deleted": session.deleted_at is not None,
            "deletedAt": _ts_ms(session.deleted_at),
            "createdAt": _ts_ms(session.created_at),
            "updatedAt": _ts_ms(session.updated_at),
            "messages": messages,
            "relatedQueries": related_queries,
        }
    }


def _csv_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def rows_to_csv(headers: list[str], rows: list[list[Any]]) -> bytes:
    lines = [",".join(_csv_escape(h) for h in headers)]
    for row in rows:
        lines.append(",".join(_csv_escape(v) for v in row))
    # Excel 友好 UTF-8 BOM
    return ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")


def format_ts_csv(ms: int | None) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def export_operations_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_operation_logs(db, limit=export_max_rows(), offset=0, **filters)
    headers = [
        "id",
        "createdAt",
        "module",
        "action",
        "operator",
        "operatorId",
        "success",
        "statusCode",
        "method",
        "path",
        "resourceId",
        "detail",
        "errorMsg",
        "ip",
        "durationMs",
        "requestId",
    ]
    rows = [
        [
            it["id"],
            format_ts_csv(it["createdAt"]),
            it["module"],
            it["action"],
            it.get("operatorUsername") or "",
            it.get("operatorId") or "",
            it["success"],
            it["statusCode"],
            it["method"],
            it["path"],
            it.get("resourceId") or "",
            it.get("detail") or "",
            it.get("errorMsg") or "",
            it.get("ip") or "",
            it.get("durationMs") or 0,
            it.get("requestId") or "",
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)


async def export_logins_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_login_logs(db, limit=export_max_rows(), offset=0, **filters)
    headers = ["id", "createdAt", "username", "userId", "success", "reason", "ip", "userAgent"]
    rows = [
        [
            it["id"],
            format_ts_csv(it["createdAt"]),
            it["username"],
            it.get("userId") or "",
            it["success"],
            it.get("reason") or "",
            it.get("ip") or "",
            it.get("userAgent") or "",
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)


async def export_knowledge_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_knowledge_audits(db, limit=export_max_rows(), offset=0, **filters)
    headers = [
        "id",
        "createdAt",
        "action",
        "operator",
        "baseId",
        "docId",
        "beforeJson",
        "afterJson",
        "ip",
        "requestId",
    ]
    rows = [
        [
            it["id"],
            format_ts_csv(it["createdAt"]),
            it["action"],
            it.get("operatorUsername") or "",
            it.get("baseId") or "",
            it.get("docId") or "",
            it.get("beforeJson") or "",
            it.get("afterJson") or "",
            it.get("ip") or "",
            it.get("requestId") or "",
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)


async def export_queries_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_data_queries(db, limit=export_max_rows(), offset=0, **filters)
    headers = [
        "id",
        "createdAt",
        "source",
        "operator",
        "queryText",
        "toolName",
        "success",
        "durationMs",
        "resultSummary",
        "sessionId",
        "requestId",
        "paramsJson",
    ]
    rows = [
        [
            it["id"],
            format_ts_csv(it["createdAt"]),
            it["source"],
            it.get("operatorUsername") or "",
            it.get("queryText") or "",
            it.get("toolName") or "",
            it["success"],
            it.get("durationMs") or 0,
            it.get("resultSummary") or "",
            it.get("sessionId") or "",
            it.get("requestId") or "",
            it.get("paramsJson") or "",
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)


async def export_chats_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_chat_audits(db, limit=export_max_rows(), offset=0, **filters)
    headers = [
        "sessionId",
        "title",
        "username",
        "userId",
        "mode",
        "messageCount",
        "deleted",
        "deletedAt",
        "createdAt",
        "updatedAt",
    ]
    rows = [
        [
            it["id"],
            it.get("title") or "",
            it.get("username") or "",
            it.get("userId") or "",
            it.get("mode") or "",
            it.get("messageCount") or 0,
            it.get("deleted"),
            format_ts_csv(it.get("deletedAt")),
            format_ts_csv(it.get("createdAt")),
            format_ts_csv(it.get("updatedAt")),
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)


def _clip_hot_value(text: str | None, limit: int = 2000) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def to_hot_config_item(row: HotConfigAudit) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.config_key,
        "action": row.action,
        "version": row.version,
        "oldValue": _clip_hot_value(row.old_value),
        "newValue": _clip_hot_value(row.new_value),
        "operatorId": row.operator_id,
        "remark": row.remark,
        "createdAt": _ts_ms(row.created_at),
    }


async def list_hot_config_audits(
    db: AsyncSession,
    *,
    action: str | None = None,
    keyword: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(HotConfigAudit)
    count_stmt = select(func.count()).select_from(HotConfigAudit)
    act = (action or "").strip()
    if act:
        stmt = stmt.where(HotConfigAudit.action == act)
        count_stmt = count_stmt.where(HotConfigAudit.action == act)
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        cond = or_(
            HotConfigAudit.config_key.like(like),
            HotConfigAudit.old_value.like(like),
            HotConfigAudit.new_value.like(like),
            HotConfigAudit.remark.like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    start = _ms_to_dt(from_ms)
    end = _ms_to_dt(to_ms)
    if start is not None:
        stmt = stmt.where(HotConfigAudit.created_at >= start)
        count_stmt = count_stmt.where(HotConfigAudit.created_at >= start)
    if end is not None:
        stmt = stmt.where(HotConfigAudit.created_at <= end)
        count_stmt = count_stmt.where(HotConfigAudit.created_at <= end)
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    result = await db.execute(
        stmt.order_by(HotConfigAudit.id.desc()).offset(offset).limit(limit)
    )
    return {
        "items": [to_hot_config_item(r) for r in result.scalars().all()],
        "total": total,
        "traceRetentionDays": trace_retention_days(),
    }


async def export_hot_configs_csv(db: AsyncSession, **filters: Any) -> bytes:
    data = await list_hot_config_audits(db, limit=export_max_rows(), offset=0, **filters)
    headers = [
        "id",
        "createdAt",
        "key",
        "action",
        "version",
        "operatorId",
        "oldValue",
        "newValue",
        "remark",
    ]
    rows = [
        [
            it["id"],
            format_ts_csv(it["createdAt"]),
            it.get("key") or "",
            it.get("action") or "",
            it.get("version") or "",
            it.get("operatorId") or "",
            it.get("oldValue") or "",
            it.get("newValue") or "",
            it.get("remark") or "",
        ]
        for it in data["items"]
    ]
    return rows_to_csv(headers, rows)
