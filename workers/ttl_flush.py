"""即将过期会话扫描：TTL < 阈值时补写 MySQL 并删除 Redis Key。"""

from __future__ import annotations

from api.services.ai import memory as memory_svc
from common.config import get_settings
from common.logging import get_logger
from common.redis_client import get_redis
from db.session import AsyncSessionLocal

logger = get_logger(__name__)


async def scan_and_flush_expiring_sessions() -> int:
    settings = get_settings()
    redis = get_redis()
    threshold = settings.chat_ttl_scan_threshold_seconds
    flushed = 0
    async for key in redis.scan_iter(match="chat:session:*", count=100):
        ttl = await redis.ttl(key)
        # ttl=-1 永不过期也应兜底；ttl=-2 不存在跳过
        if ttl == -2:
            continue
        if ttl != -1 and ttl >= threshold:
            continue
        session_id = str(key).removeprefix("chat:session:")
        try:
            async with AsyncSessionLocal() as db:
                n = await memory_svc.flush_expiring_session_to_mysql(
                    db, session_public_id=session_id
                )
                flushed += n
        except Exception:  # noqa: BLE001
            logger.exception("ttl flush failed session=%s", session_id)
    logger.info("ttl scan done flushed_messages=%s", flushed)
    return flushed
