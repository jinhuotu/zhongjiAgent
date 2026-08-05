from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession
from api.services import production as prod_svc
from common.response import ok

router = APIRouter(prefix="/production", tags=["production"])


class IssueCommandRequest(BaseModel):
    tagCode: str = Field(min_length=1, max_length=64)
    targetValue: float | None = None
    targetText: str | None = None
    executor: str = "simulate"  # simulate | plc


@router.get("/systems")
async def systems_list(db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok({"items": await prod_svc.list_systems(db)})


@router.get("/{system_code}/snapshot")
async def system_snapshot(system_code: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok(await prod_svc.get_snapshot(db, system_code))


@router.get("/{system_code}/tags")
async def system_tags(system_code: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    return ok({"items": await prod_svc.list_tags(db, system_code)})


@router.get("/{system_code}/series")
async def system_series(
    system_code: str,
    db: DbSession,
    user: CurrentUser,
    tags: str = Query(..., description="comma-separated tag codes"),
    hours: int = 24,
    limit: int = 500,
) -> dict:
    _ = user
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return ok(
        await prod_svc.get_series(
            db,
            system_code,
            tag_codes=tag_list,
            hours=hours,
            limit=limit,
        )
    )


@router.get("/{system_code}/alarms")
async def system_alarms(
    system_code: str,
    db: DbSession,
    user: CurrentUser,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    _ = user
    return ok(
        {
            "items": await prod_svc.list_alarms(
                db, system_code, status=status, limit=limit
            )
        }
    )


@router.post("/alarms/{alarm_id}/ack")
async def alarm_ack(alarm_id: str, db: DbSession, user: CurrentUser) -> dict:
    item = await prod_svc.ack_alarm(db, public_id=alarm_id, user_id=user.id)
    return ok({"item": item})


@router.get("/{system_code}/commands")
async def system_commands(
    system_code: str,
    db: DbSession,
    user: CurrentUser,
    limit: int = 30,
) -> dict:
    _ = user
    return ok({"items": await prod_svc.list_commands(db, system_code, limit=limit)})


@router.post("/{system_code}/commands")
async def system_issue_command(
    system_code: str,
    body: IssueCommandRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    item = await prod_svc.issue_command(
        db,
        system_code=system_code,
        tag_code=body.tagCode,
        user_id=user.id,
        target_value=body.targetValue,
        target_text=body.targetText,
        executor_mode=body.executor or "simulate",
    )
    return ok({"item": item})
