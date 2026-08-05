from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.ai import memory as memory_svc
from api.services.models.runtime import build_llm_client
from common.errors import AppError, ErrorCode
from db.models.chat import ChatSession


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def _ts_ms(dt: datetime | None) -> int:
    if not dt:
        return 0
    return int(dt.timestamp() * 1000)


def to_session_item(
    session: ChatSession,
    *,
    include_messages: bool = False,
    messages_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": session.public_id,
        "title": session.title,
        "titleAuto": bool(session.title_auto),
        "mode": session.mode,
        "summary": session.summary,
        "messageCount": session.message_count,
        "lastMessageAt": _ts_ms(session.last_message_at),
        "createdAt": _ts_ms(session.created_at),
        "updatedAt": _ts_ms(session.updated_at),
    }
    if include_messages:
        if messages_override is not None:
            item["messages"] = messages_override
        else:
            msgs = list(session.messages or [])
            item["messages"] = [
                {
                    "id": m.public_id,
                    "role": m.role,
                    "content": m.content,
                    "mode": m.mode,
                    "refs": m.refs or [],
                    "createdAt": _ts_ms(m.created_at),
                }
                for m in msgs
            ]
    return item


async def list_sessions(db: AsyncSession, *, user_id: int) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc())
    )
    return [to_session_item(s) for s in result.scalars().all()]


async def create_session(
    db: AsyncSession,
    *,
    user_id: int,
    title: str | None = None,
    mode: str = "fast",
) -> dict[str, Any]:
    name = (title or "").strip() or "新对话"
    session = ChatSession(
        public_id=short_id(12),
        user_id=user_id,
        title=name[:128],
        title_auto=not bool((title or "").strip()),
        mode=mode if mode in ("fast", "deep") else "fast",
        message_count=0,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return to_session_item(session, include_messages=True)


async def get_session_for_user(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
    with_messages: bool = True,
) -> ChatSession:
    stmt = select(ChatSession).where(
        ChatSession.public_id == public_id,
        ChatSession.user_id == user_id,
    )
    if with_messages:
        stmt = stmt.options(selectinload(ChatSession.messages))
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise AppError(ErrorCode.NOT_FOUND, "session not found", status_code=404)
    return session


async def update_session(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
    title: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    session = await get_session_for_user(db, public_id=public_id, user_id=user_id, with_messages=False)
    if title is not None:
        name = title.strip()
        if not name:
            raise AppError(ErrorCode.VALIDATION, "title is required", status_code=422)
        session.title = name[:128]
        session.title_auto = False
    if mode is not None:
        if mode not in ("fast", "deep"):
            raise AppError(ErrorCode.VALIDATION, "invalid mode", status_code=422)
        session.mode = mode
    await db.commit()
    await db.refresh(session)
    return to_session_item(session)


async def delete_session(db: AsyncSession, *, public_id: str, user_id: int) -> None:
    session = await get_session_for_user(db, public_id=public_id, user_id=user_id, with_messages=False)
    await memory_svc.delete_hot_session(public_id)
    await db.delete(session)
    await db.commit()


async def get_session_item_merged(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> dict[str, Any]:
    """打开会话：MySQL 归档 + Redis 未入库尾部合并。"""
    session = await get_session_for_user(
        db, public_id=public_id, user_id=user_id, with_messages=False
    )
    merged = await memory_svc.merge_messages_for_api(db, session=session)
    return to_session_item(session, include_messages=True, messages_override=merged)


async def summarize_session_title(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
    force: bool = False,
) -> dict[str, Any]:
    session, hot_or_db_msgs = await memory_svc.load_session_with_messages_for_title(
        db, public_id=public_id, user_id=user_id
    )
    if not force and not session.title_auto:
        return to_session_item(session, include_messages=False)

    texts: list[str] = []
    for m in hot_or_db_msgs:
        role = str(m.get("role") if isinstance(m, dict) else getattr(m, "role", ""))
        content = str(m.get("content") if isinstance(m, dict) else getattr(m, "content", ""))
        if role in ("user", "assistant") and content.strip():
            texts.append(f"{role}: {content.strip()[:200]}")
        if len(texts) >= 4:
            break
    if not texts:
        raise AppError(ErrorCode.VALIDATION, "session has no messages to summarize", status_code=422)

    prompt = (
        "根据以下工业窑炉领域的问答对话，生成一个简洁中文会话标题。"
        "要求：不超过 18 个字；不要引号；不要标点结尾；概括主题即可。\n\n"
        + "\n".join(texts)
    )
    client = await build_llm_client(db, "fast")
    raw = await client.complete(
        [
            {"role": "system", "content": "你是标题生成助手，只输出标题本身。"},
            {"role": "user", "content": prompt},
        ],
        mode="fast",
    )
    title = raw.strip().strip("\"'「」《》").replace("\n", " ")[:18] or "新对话"
    session.title = title
    session.title_auto = True
    session.summary = title
    await db.commit()
    await db.refresh(session)
    return to_session_item(session)


async def maybe_auto_title(db: AsyncSession, *, session: ChatSession) -> str | None:
    """首轮问答后自动生成标题；失败则静默忽略。"""
    if not session.title_auto:
        return None
    if (session.message_count or 0) < 2:
        return None
    if session.title not in ("新对话", "未命名会话", "") and (session.message_count or 0) > 2:
        return None
    try:
        item = await summarize_session_title(
            db,
            public_id=session.public_id,
            user_id=session.user_id,
            force=True,
        )
        return str(item.get("title") or "")
    except Exception:  # noqa: BLE001
        return None
