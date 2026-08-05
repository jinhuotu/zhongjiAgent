"""对话热记忆（Redis List）+ Stream 投递。

Key 规范：
  chat:session:{session_public_id}  → Redis List，元素为 JSON 消息
  agent:chat:stream                 → Redis Stream，待异步归档
  agent:chat:stream:dql             → 死信队列

规则（M1）：
  - 热数据仅活跃会话，TTL 默认 7 天；MySQL 为唯一可信归档源
  - 接口路径只保证 List + Stream 写入成功，不在请求内同步写消息表
  - 冷启动：List 不存在时从 MySQL 回填最近 N 轮（user+assistant 一对算一轮）
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from common.config import get_settings
from common.logging import get_logger
from common.redis_client import get_redis
from db.models.chat import ChatMessage, ChatSession

logger = get_logger(__name__)


def session_key(session_public_id: str) -> str:
    return f"chat:session:{session_public_id}"


def short_msg_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def build_hot_message(
    *,
    role: str,
    content: str,
    mode: str | None = None,
    refs: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_output: Any = None,
    tool_error: str | None = None,
    tool_duration_ms: int | None = None,
    msg_id: str | None = None,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    """构造写入 Redis List / Stream 的统一消息结构。"""
    return {
        "id": msg_id or short_msg_id(12),
        "role": role,
        "content": content,
        "mode": mode,
        "refs": refs or [],
        "modelName": model_name,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "toolName": tool_name,
        "toolInput": tool_input,
        "toolOutput": tool_output,
        "toolError": tool_error,
        "toolDurationMs": tool_duration_ms,
        "createdAt": created_at_ms or int(time.time() * 1000),
    }


def hot_message_to_api(msg: dict[str, Any]) -> dict[str, Any]:
    """转为前端会话消息结构。"""
    return {
        "id": msg.get("id"),
        "role": msg.get("role"),
        "content": msg.get("content") or "",
        "mode": msg.get("mode"),
        "refs": msg.get("refs") or [],
        "createdAt": int(msg.get("createdAt") or 0),
        "toolName": msg.get("toolName"),
    }


async def redis_session_exists(session_public_id: str) -> bool:
    redis = get_redis()
    return bool(await redis.exists(session_key(session_public_id)))


async def load_hot_messages(session_public_id: str) -> list[dict[str, Any]]:
    redis = get_redis()
    raw_items = await redis.lrange(session_key(session_public_id), 0, -1)
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("skip invalid hot message in %s", session_public_id)
    return out


async def _touch_ttl(session_public_id: str) -> None:
    settings = get_settings()
    redis = get_redis()
    await redis.expire(session_key(session_public_id), settings.chat_session_ttl_seconds)


async def append_hot_and_enqueue(
    *,
    session: ChatSession,
    message: dict[str, Any],
) -> str:
    """① RPUSH 热会话 ② XADD Stream，返回 stream_msg_id。

    主业务接口只依赖本函数成功；MySQL 由独立 Worker 异步归档。
    """
    settings = get_settings()
    redis = get_redis()
    key = session_key(session.public_id)
    payload = json.dumps(message, ensure_ascii=False)

    pipe = redis.pipeline(transaction=True)
    pipe.rpush(key, payload)
    pipe.expire(key, settings.chat_session_ttl_seconds)
    await pipe.execute()

    stream_fields = {
        "sessionId": session.public_id,
        "sessionDbId": str(session.id),
        "userId": str(session.user_id),
        "message": payload,
        "enqueuedAt": str(int(time.time() * 1000)),
    }
    stream_id = await redis.xadd(settings.chat_stream_key, stream_fields)
    logger.debug(
        "enqueued chat msg session=%s role=%s stream_id=%s",
        session.public_id,
        message.get("role"),
        stream_id,
    )
    return str(stream_id)


async def delete_hot_session(session_public_id: str) -> None:
    redis = get_redis()
    await redis.delete(session_key(session_public_id))


async def cold_start_from_mysql(
    db: AsyncSession,
    *,
    session: ChatSession,
) -> list[dict[str, Any]]:
    """Redis miss 时从 MySQL 回填最近 N 轮到 List。"""
    settings = get_settings()
    # 一轮 = user+assistant，最多取 2N 条消息
    limit = max(2, settings.chat_cold_start_turns * 2)

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )
    rows = list(reversed(result.scalars().all()))
    if not rows:
        return []

    hot_msgs = [
        build_hot_message(
            role=m.role,
            content=m.content,
            mode=m.mode,
            refs=m.refs if isinstance(m.refs, list) else [],
            model_name=m.model_name,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            total_tokens=m.total_tokens,
            tool_name=m.tool_name,
            tool_input=m.tool_input,
            tool_output=m.tool_output,
            tool_error=m.tool_error,
            tool_duration_ms=m.tool_duration_ms,
            msg_id=m.public_id,
            created_at_ms=int(m.created_at.timestamp() * 1000) if m.created_at else None,
        )
        for m in rows
    ]

    redis = get_redis()
    key = session_key(session.public_id)
    # 避免并发冷启动重复写入：仅在 key 仍不存在时灌入
    if await redis.exists(key):
        return await load_hot_messages(session.public_id)

    pipe = redis.pipeline(transaction=True)
    for msg in hot_msgs:
        pipe.rpush(key, json.dumps(msg, ensure_ascii=False))
    pipe.expire(key, settings.chat_session_ttl_seconds)
    await pipe.execute()
    logger.info(
        "cold-start session=%s filled=%s from mysql",
        session.public_id,
        len(hot_msgs),
    )
    return hot_msgs


async def ensure_hot_context(
    db: AsyncSession,
    *,
    session: ChatSession,
) -> list[dict[str, Any]]:
    """保证 Redis 有会话上下文；必要时冷启动。"""
    if await redis_session_exists(session.public_id):
        await _touch_ttl(session.public_id)
        return await load_hot_messages(session.public_id)
    return await cold_start_from_mysql(db, session=session)


def hot_messages_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """转为 OpenAI chat messages（仅 user/assistant/system；tool 暂拼进 content 备注）。"""
    out: list[dict[str, str]] = []
    for m in messages:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if role in ("user", "assistant", "system"):
            out.append({"role": role, "content": content})
        elif role == "tool":
            # M1：无原生 tools 协议时，把 tool 结果折叠为 system 备注，保证多轮连续
            name = m.get("toolName") or "tool"
            out.append(
                {
                    "role": "system",
                    "content": f"[tool:{name}] {content}",
                }
            )
    return out


async def merge_messages_for_api(
    db: AsyncSession,
    *,
    session: ChatSession,
) -> list[dict[str, Any]]:
    """打开会话时：MySQL 全量 + Redis 未入库尾部（按 id 去重）。"""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.id.asc())
    )
    mysql_msgs = result.scalars().all()
    seen = {m.public_id for m in mysql_msgs}
    api_msgs = [
        {
            "id": m.public_id,
            "role": m.role,
            "content": m.content,
            "mode": m.mode,
            "refs": m.refs or [],
            "createdAt": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
            "toolName": m.tool_name,
        }
        for m in mysql_msgs
    ]

    if await redis_session_exists(session.public_id):
        for hm in await load_hot_messages(session.public_id):
            mid = hm.get("id")
            if mid and mid not in seen:
                api_msgs.append(hot_message_to_api(hm))
                seen.add(str(mid))
    return api_msgs


async def bump_session_meta(
    db: AsyncSession,
    *,
    session: ChatSession,
    mode: str | None = None,
) -> None:
    """轻量更新会话元数据（非消息正文），便于侧栏排序；可同步。"""
    session.message_count = int(session.message_count or 0) + 1
    session.last_message_at = datetime.now(timezone.utc)
    if mode in ("fast", "deep"):
        session.mode = mode
    await db.commit()
    await db.refresh(session)


async def load_session_with_messages_for_title(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> tuple[ChatSession, list[dict[str, Any]]]:
    """标题生成：优先 Redis 热消息，否则 MySQL。"""
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.public_id == public_id, ChatSession.user_id == user_id)
        .options(selectinload(ChatSession.messages))
    )
    session = result.scalar_one_or_none()
    if session is None:
        from common.errors import AppError, ErrorCode

        raise AppError(ErrorCode.NOT_FOUND, "session not found", status_code=404)

    if await redis_session_exists(session.public_id):
        return session, await load_hot_messages(session.public_id)
    return session, [
        {
            "role": m.role,
            "content": m.content,
        }
        for m in (session.messages or [])
    ]


def count_turns(messages: list[dict[str, Any]]) -> int:
    """一轮 = 一条 user 消息（与 assistant/tool 配对）。"""
    return sum(1 for m in messages if str(m.get("role")) == "user")


def _keep_start_index(messages: list[dict[str, Any]], keep_turns: int) -> int:
    """返回保留窗口的起始下标：从末尾数 keep_turns 个 user。"""
    if keep_turns <= 0:
        return len(messages)
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if str(messages[i].get("role")) == "user":
            seen += 1
            if seen >= keep_turns:
                return i
    return 0


async def maybe_roll_trim(
    db: AsyncSession,
    *,
    session: ChatSession,
) -> dict[str, Any] | None:
    """超长对话滚动裁剪：> trigger 轮时仅保留最近 keep 轮，旧对话摘要入 Qdrant。

    被裁剪消息此前已 XADD Stream，MySQL 由 Worker 归档；此处只 LTRIM Redis。
    """
    settings = get_settings()
    messages = await load_hot_messages(session.public_id)
    turns = count_turns(messages)
    if turns <= settings.chat_trim_trigger_turns:
        return None

    start = _keep_start_index(messages, settings.chat_trim_keep_turns)
    if start <= 0:
        return None
    dropped = messages[:start]
    kept = messages[start:]

    # 摘要（优先 LLM；失败则截断拼接）
    summary = await _summarize_dropped(db, dropped)

    try:
        from api.services.ai.long_memory import store_trim_summary

        await store_trim_summary(
            db,
            session_id=session.public_id,
            user_id=session.user_id,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("store trim summary failed: %s", exc)

    redis = get_redis()
    key = session_key(session.public_id)
    pipe = redis.pipeline(transaction=True)
    pipe.delete(key)
    # 可选：在窗口前插入一条 system 摘要，帮助 LLM 连贯
    if summary.strip():
        sys_msg = build_hot_message(
            role="system",
            content=f"【历史对话摘要】{summary.strip()}",
            mode=session.mode,
        )
        pipe.rpush(key, json.dumps(sys_msg, ensure_ascii=False))
    for msg in kept:
        pipe.rpush(key, json.dumps(msg, ensure_ascii=False))
    pipe.expire(key, settings.chat_session_ttl_seconds)
    await pipe.execute()

    # 会话 summary 字段保留最近摘要（可加长）
    session.summary = (summary or "")[:512] or session.summary
    await db.commit()

    logger.info(
        "roll-trim session=%s dropped=%s kept=%s turns_before=%s",
        session.public_id,
        len(dropped),
        len(kept),
        turns,
    )
    return {"dropped": len(dropped), "kept": len(kept), "summary": summary}


async def _summarize_dropped(db: AsyncSession, dropped: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for m in dropped:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            texts.append(f"{role}: {content[:300]}")
        if len(texts) >= 24:
            break
    if not texts:
        return ""
    blob = "\n".join(texts)
    try:
        from api.services.models.runtime import build_llm_client

        client = await build_llm_client(db, "fast")
        raw = await client.complete(
            [
                {
                    "role": "system",
                    "content": "你是对话摘要助手。用中文概括历史对话要点，不超过 200 字，不要列表符号过多。",
                },
                {"role": "user", "content": blob[:6000]},
            ],
            mode="fast",
        )
        return (raw or "").strip()[:500]
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm summarize dropped failed: %s", exc)
        return blob[:400]


async def flush_expiring_session_to_mysql(
    db: AsyncSession,
    *,
    session_public_id: str,
) -> int:
    """TTL 兜底：将 Redis 会话中尚未入库的消息补写 MySQL，成功后删 Key。"""
    if not await redis_session_exists(session_public_id):
        return 0
    result = await db.execute(
        select(ChatSession).where(ChatSession.public_id == session_public_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        await delete_hot_session(session_public_id)
        return 0

    hot = await load_hot_messages(session_public_id)
    if not hot:
        await delete_hot_session(session_public_id)
        return 0

    existing = await db.execute(
        select(ChatMessage.public_id).where(ChatMessage.session_id == session.id)
    )
    have = {x for x in existing.scalars().all()}
    written = 0
    for msg in hot:
        mid = str(msg.get("id") or "")
        if not mid or mid in have:
            continue
        # 使用 public_id 作为幂等；stream_msg_id 用 ttl 前缀避免与 Stream 冲突
        stream_id = f"ttl-{mid}"
        dup = await db.execute(
            select(ChatMessage.id).where(ChatMessage.stream_msg_id == stream_id)
        )
        if dup.scalar_one_or_none() is not None:
            continue
        created_ms = int(msg.get("createdAt") or 0)
        created_at = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
            if created_ms > 0
            else datetime.now(timezone.utc)
        )
        db.add(
            ChatMessage(
                public_id=mid[:32],
                session_id=session.id,
                role=str(msg.get("role") or "user")[:16],
                content=str(msg.get("content") or ""),
                mode=msg.get("mode"),
                refs=msg.get("refs") if isinstance(msg.get("refs"), list) else None,
                stream_msg_id=stream_id,
                model_name=msg.get("modelName"),
                prompt_tokens=msg.get("promptTokens"),
                completion_tokens=msg.get("completionTokens"),
                total_tokens=msg.get("totalTokens"),
                tool_name=msg.get("toolName"),
                tool_input=msg.get("toolInput"),
                tool_output=msg.get("toolOutput"),
                tool_error=msg.get("toolError"),
                tool_duration_ms=msg.get("toolDurationMs"),
                created_at=created_at,
            )
        )
        written += 1
    await db.commit()
    await delete_hot_session(session_public_id)
    logger.info("ttl-flush session=%s wrote=%s", session_public_id, written)
    return written
