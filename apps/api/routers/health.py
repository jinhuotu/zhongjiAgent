from fastapi import APIRouter

from common.response import ok

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return ok(
        {
            "status": "up",
            "service": "zhongji-api",
        }
    )
