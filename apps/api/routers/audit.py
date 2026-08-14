from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import AdminUser, DbSession
from api.services import audit as audit_svc
from common.response import ok

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/summary")
async def audit_summary(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok(await audit_svc.summary(db))


@router.get("/operations")
async def audit_operations(
    db: DbSession,
    admin: AdminUser,
    module: str | None = Query(default=None),
    success: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = admin
    return ok(
        await audit_svc.list_operation_logs(
            db,
            module=module,
            success=success,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/logins")
async def audit_logins(
    db: DbSession,
    admin: AdminUser,
    success: bool | None = Query(default=None),
    username: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _ = admin
    return ok(
        await audit_svc.list_login_logs(
            db,
            success=success,
            username=username,
            limit=limit,
            offset=offset,
        )
    )
