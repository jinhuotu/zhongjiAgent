from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import CurrentUser, DbSession
from api.services import alerts as alerts_svc
from common.response import ok

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def alerts_list(
    db: DbSession,
    user: CurrentUser,
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    _ = user
    data = await alerts_svc.list_alerts(
        db, status=status, severity=severity, limit=limit
    )
    return ok(data)


@router.get("/rules")
async def alerts_rules(db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok({"items": await alerts_svc.list_rules(db)})


@router.post("/{alert_id}/ack")
async def alerts_ack(alert_id: str, db: DbSession, user: CurrentUser) -> dict:
    item = await alerts_svc.ack_alert(db, public_id=alert_id, user_id=user.id)
    return ok({"item": item})


@router.post("/{alert_id}/close")
async def alerts_close(alert_id: str, db: DbSession, user: CurrentUser) -> dict:
    item = await alerts_svc.close_alert(db, public_id=alert_id, user_id=user.id)
    return ok({"item": item})
