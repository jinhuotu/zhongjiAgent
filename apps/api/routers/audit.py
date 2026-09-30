from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import Response

from api.deps import AuditReader, DbSession
from api.services import audit as audit_svc
from common.errors import AppError, ErrorCode
from common.response import ok

router = APIRouter(prefix="/audit", tags=["audit"])


def _csv_response(content: bytes, filename: str) -> Response:
    safe = quote(filename)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/summary")
async def audit_summary(db: DbSession, reader: AuditReader) -> dict:
    _ = reader
    return ok(await audit_svc.summary(db))


@router.get("/operations")
async def audit_operations(
    db: DbSession,
    reader: AuditReader,
    module: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    username: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_operation_logs(
            db,
            module=module,
            success=success,
            keyword=keyword,
            username=username,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/operations/export")
async def audit_operations_export(
    db: DbSession,
    reader: AuditReader,
    module: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    username: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_operations_csv(
        db,
        module=module,
        success=success,
        keyword=keyword,
        username=username,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-operations.csv")


@router.get("/logins")
async def audit_logins(
    db: DbSession,
    reader: AuditReader,
    success: bool | None = Query(default=None),
    username: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_login_logs(
            db,
            success=success,
            username=username,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/logins/export")
async def audit_logins_export(
    db: DbSession,
    reader: AuditReader,
    success: bool | None = Query(default=None),
    username: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_logins_csv(
        db,
        success=success,
        username=username,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-logins.csv")


@router.get("/knowledge")
async def audit_knowledge(
    db: DbSession,
    reader: AuditReader,
    action: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_knowledge_audits(
            db,
            action=action,
            keyword=keyword,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/knowledge/export")
async def audit_knowledge_export(
    db: DbSession,
    reader: AuditReader,
    action: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_knowledge_csv(
        db,
        action=action,
        keyword=keyword,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-knowledge.csv")


@router.get("/queries")
async def audit_queries(
    db: DbSession,
    reader: AuditReader,
    source: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_data_queries(
            db,
            source=source,
            success=success,
            keyword=keyword,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/queries/export")
async def audit_queries_export(
    db: DbSession,
    reader: AuditReader,
    source: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_queries_csv(
        db,
        source=source,
        success=success,
        keyword=keyword,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-queries.csv")


@router.get("/hot-configs")
async def audit_hot_configs(
    db: DbSession,
    reader: AuditReader,
    action: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_hot_config_audits(
            db,
            action=action,
            keyword=keyword,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/hot-configs/export")
async def audit_hot_configs_export(
    db: DbSession,
    reader: AuditReader,
    action: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_hot_configs_csv(
        db,
        action=action,
        keyword=keyword,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-hot-configs.csv")


@router.get("/chats")
async def audit_chats(
    db: DbSession,
    reader: AuditReader,
    username: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    session_id: str | None = Query(default=None, alias="sessionId"),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = reader
    return ok(
        await audit_svc.list_chat_audits(
            db,
            username=username,
            keyword=keyword,
            session_id=session_id,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/chats/export")
async def audit_chats_export(
    db: DbSession,
    reader: AuditReader,
    username: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    session_id: str | None = Query(default=None, alias="sessionId"),
    from_ms: int | None = Query(default=None, alias="from"),
    to_ms: int | None = Query(default=None, alias="to"),
) -> Response:
    _ = reader
    content = await audit_svc.export_chats_csv(
        db,
        username=username,
        keyword=keyword,
        session_id=session_id,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return _csv_response(content, "audit-chats.csv")


@router.get("/chats/{session_id}")
async def audit_chat_detail(session_id: str, db: DbSession, reader: AuditReader) -> dict:
    _ = reader
    data = await audit_svc.get_chat_audit(db, session_id=session_id)
    if not data:
        raise AppError(ErrorCode.NOT_FOUND, "session not found", status_code=404)
    return ok(data)
