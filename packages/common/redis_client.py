"""Redis 异步客户端单例。

连接串从 Settings.redis_url 读取（.env: REDIS_URL）。
Docker Compose 默认：宿主机 redis://127.0.0.1:16379/0。
"""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from common.config import get_settings


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def close_redis() -> None:
    client = get_redis()
    await client.aclose()
    get_redis.cache_clear()
