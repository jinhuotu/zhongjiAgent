"""Redis 通用能力：滑动窗口限流、会话锁、Embedding 缓存。

Key 规范：
  ratelimit:chat:{user_id}           ZSet（score=时间戳）
  ratelimit:mcp:{user_id}            ZSet（预留）
  lock:chat:session:{session_id}     String（SET NX EX）
  embed:cache:{sha256(model|text)}   String（JSON float[]）
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from common.config import get_settings
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from common.redis_client import get_redis

logger = get_logger(__name__)

_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


async def check_sliding_rate_limit(
    *,
    scope: str,
    subject: str,
    max_requests: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """ZSet 滑动窗口限流；超限抛 429。"""
    settings = get_settings()
    limit = max_requests if max_requests is not None else settings.chat_rate_limit_max
    window = (
        window_seconds
        if window_seconds is not None
        else settings.chat_rate_limit_window_seconds
    )
    if limit <= 0:
        return

    redis = get_redis()
    key = f"ratelimit:{scope}:{subject}"
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex}"
    pipe = redis.pipeline(transaction=True)
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {member: now})
    pipe.zcard(key)
    pipe.expire(key, window + 1)
    _rem, _add, count, _exp = await pipe.execute()
    if int(count) > limit:
        # 回滚本次计数
        await redis.zrem(key, member)
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"请求过于频繁，请 {window} 秒后再试",
            status_code=429,
        )


_RENEW_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
else
  return 0
end
"""


class SessionLock:
    """单 Redis 实例下的会话互斥锁（集群多 API 实例防并发重复调 LLM）。"""

    def __init__(self, session_id: str, *, ttl_seconds: int | None = None) -> None:
        settings = get_settings()
        self.key = f"lock:chat:session:{session_id}"
        self.ttl = ttl_seconds or settings.chat_session_lock_ttl_seconds
        self.token = secrets.token_hex(16)
        self._held = False

    async def acquire(self, *, wait_seconds: float = 0.0) -> bool:
        redis = get_redis()
        deadline = time.time() + max(0.0, wait_seconds)
        while True:
            ok = await redis.set(self.key, self.token, nx=True, ex=self.ttl)
            if ok:
                self._held = True
                return True
            if time.time() >= deadline:
                return False
            await __import__("asyncio").sleep(0.05)

    async def renew(self) -> bool:
        """延长锁 TTL；仅当仍由本 token 持有时成功。"""
        if not self._held:
            return False
        redis = get_redis()
        try:
            ok = await redis.eval(_RENEW_LOCK_LUA, 1, self.key, self.token, str(int(self.ttl)))
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session lock renew failed key=%s: %s", self.key, exc)
            return False

    async def release(self) -> None:
        if not self._held:
            return
        redis = get_redis()
        try:
            await redis.eval(_RELEASE_LOCK_LUA, 1, self.key, self.token)
        finally:
            self._held = False


async def force_release_session_lock(session_id: str) -> bool:
    """强制删除会话锁（用户点停止 / 客户端断开时用，不校验 token）。"""
    sid = (session_id or "").strip()
    if not sid:
        return False
    redis = get_redis()
    key = f"lock:chat:session:{sid}"
    try:
        deleted = await redis.delete(key)
        if deleted:
            logger.info("force released chat session lock key=%s", key)
        return bool(deleted)
    except Exception as exc:  # noqa: BLE001
        logger.warning("force release session lock failed key=%s: %s", key, exc)
        return False


@asynccontextmanager
async def session_lock(
    session_id: str,
    *,
    wait_seconds: float = 15.0,
) -> AsyncIterator[SessionLock]:
    lock = SessionLock(session_id)
    acquired = await lock.acquire(wait_seconds=wait_seconds)
    if not acquired:
        raise AppError(
            ErrorCode.CONFLICT,
            "该会话正在生成回复，请稍后再试（若刚点过停止，请再试一次或新建会话）",
            status_code=409,
        )
    try:
        yield lock
    finally:
        await lock.release()


def _embed_cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(f"{model}|{text}".encode("utf-8")).hexdigest()
    return f"embed:cache:{digest}"


async def get_cached_embedding(model: str, text: str) -> list[float] | None:
    redis = get_redis()
    raw = await redis.get(_embed_cache_key(model, text))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [float(x) for x in data]
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


async def set_cached_embedding(model: str, text: str, vector: list[float]) -> None:
    settings = get_settings()
    redis = get_redis()
    await redis.set(
        _embed_cache_key(model, text),
        json.dumps(vector),
        ex=settings.chat_embed_cache_ttl_seconds,
    )


async def get_cached_embeddings_batch(
    model: str, texts: list[str]
) -> tuple[list[list[float] | None], list[int]]:
    """返回与 texts 等长的向量列表（未命中为 None）及未命中下标。"""
    redis = get_redis()
    keys = [_embed_cache_key(model, t) for t in texts]
    if not keys:
        return [], []
    raws = await redis.mget(keys)
    vectors: list[list[float] | None] = []
    missing: list[int] = []
    for i, raw in enumerate(raws):
        if not raw:
            vectors.append(None)
            missing.append(i)
            continue
        try:
            data = json.loads(raw)
            vectors.append([float(x) for x in data] if isinstance(data, list) else None)
            if vectors[-1] is None:
                missing.append(i)
        except (json.JSONDecodeError, TypeError, ValueError):
            vectors.append(None)
            missing.append(i)
    return vectors, missing


async def set_cached_embeddings_batch(
    model: str, texts: list[str], vectors: list[list[float]]
) -> None:
    if not texts:
        return
    settings = get_settings()
    redis = get_redis()
    pipe = redis.pipeline(transaction=False)
    for text, vec in zip(texts, vectors, strict=True):
        pipe.set(
            _embed_cache_key(model, text),
            json.dumps(vec),
            ex=settings.chat_embed_cache_ttl_seconds,
        )
    await pipe.execute()
