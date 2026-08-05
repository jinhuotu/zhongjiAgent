from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import CurrentUser, DbSession
from api.services import furnaces as furnaces_svc
from common.response import ok

router = APIRouter(prefix="/furnaces", tags=["furnaces"])


@router.get("")
async def furnaces_list(
    db: DbSession,
    user: CurrentUser,
    lite: bool = Query(default=False, description="轻量列表：跳过全表 COUNT，适合下拉选择"),
) -> dict:
    _ = user
    items = await furnaces_svc.list_furnaces(db, lite=lite)
    return ok({"items": items})


@router.get("/{code}")
async def furnaces_get(code: str, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    item = await furnaces_svc.get_furnace(db, code)
    return ok({"item": item})


@router.get("/{code}/series")
async def furnaces_series(
    code: str,
    db: DbSession,
    user: CurrentUser,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    stepMinutes: int = Query(default=1, ge=1, le=60),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> dict:
    _ = user
    data = await furnaces_svc.get_series(
        db,
        code,
        from_ts=from_,
        to_ts=to,
        step_minutes=stepMinutes,
        limit=limit,
    )
    return ok(data)


@router.get("/{code}/snapshot")
async def furnaces_snapshot(
    code: str,
    db: DbSession,
    user: CurrentUser,
    at: str | None = Query(default=None),
    offsetMinutes: int | None = Query(default=None, ge=0),
) -> dict:
    _ = user
    data = await furnaces_svc.get_snapshot(
        db,
        code,
        at=at,
        offset_minutes=offsetMinutes,
    )
    return ok(data)
