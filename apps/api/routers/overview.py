from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import CurrentUser, DbSession
from api.services import overview as overview_svc
from common.response import ok

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("")
async def overview_get(
    db: DbSession,
    user: CurrentUser,
    kilnCode: str | None = Query(default="TC-03"),
) -> dict:
    _ = user
    data = await overview_svc.get_overview(db, kiln_code=kilnCode)
    return ok(data)
