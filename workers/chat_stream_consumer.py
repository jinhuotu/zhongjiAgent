"""Redis Stream 消费组：批量归档对话消息到 MySQL。

能力：
  - XREADGROUP 长轮询批量拉取
  - stream_msg_id 幂等防重
  - 失败 XADD 死信队列 + XACK（避免毒消息卡死）
  - SIGINT/SIGTERM 优雅退出
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

from sqlalchemy import select

from common.config import get_settings
from common.logging import get_logger, setup_logging
from common.redis_client import close_redis, get_redis
from db.models.chat import ChatMessage, ChatSession
from db.session import AsyncSessionLocal

logger = get_logger(__name__)


class ChatStreamConsumer:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = get_redis()
        self.consumer_name = (
            f"{self.settings.chat_stream_consumer_prefix}-{os.getpid()}"
        )
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        self._stopping.set()

    async def ensure_group(self) -> None:
        """创建消费组（已存在则忽略）。"""
        try:
            await self.redis.xgroup_create(
                name=self.settings.chat_stream_key,
                groupname=self.settings.chat_stream_group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "created stream group %s on %s",
                self.settings.chat_stream_group,
                self.settings.chat_stream_key,
            )
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" in str(exc):
                logger.info("stream group already exists: %s", self.settings.chat_stream_group)
            else:
                raise

    async def run_forever(self) -> None:
        await self.ensure_group()
        logger.info(
            "chat stream consumer started name=%s batch=%s",
            self.consumer_name,
            self.settings.chat_stream_batch_size,
        )
        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("poll cycle failed; sleep and retry")
                await asyncio.sleep(2)
        logger.info("chat stream consumer stopped")

    async def _poll_once(self) -> None:
        resp = await self.redis.xreadgroup(
            groupname=self.settings.chat_stream_group,
            consumername=self.consumer_name,
            streams={self.settings.chat_stream_key: ">"},
            count=self.settings.chat_stream_batch_size,
            block=self.settings.chat_stream_block_ms,
        )
        if not resp:
            return

        for _stream_name, messages in resp:
            for msg_id, fields in messages:
                try:
                    await self._archive_one(str(msg_id), fields)
                    await self.redis.xack(
                        self.settings.chat_stream_key,
                        self.settings.chat_stream_group,
                        msg_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("archive failed id=%s: %s", msg_id, exc)
                    await self._to_dlq(str(msg_id), fields, str(exc))
                    await self.redis.xack(
                        self.settings.chat_stream_key,
                        self.settings.chat_stream_group,
                        msg_id,
                    )

    async def _to_dlq(self, msg_id: str, fields: dict[str, Any], err: str) -> None:
        payload = {k: (v if isinstance(v, str) else str(v)) for k, v in fields.items()}
        payload["originId"] = msg_id
        payload["error"] = err[:2000]
        await self.redis.xadd(self.settings.chat_stream_dlq_key, payload)

    async def _archive_one(self, stream_msg_id: str, fields: dict[str, Any]) -> None:
        message_raw = fields.get("message") or "{}"
        session_public_id = fields.get("sessionId") or ""
        session_db_id_raw = fields.get("sessionDbId") or ""
        try:
            message = json.loads(message_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid message json: {exc}") from exc

        async with AsyncSessionLocal() as db:
            # 幂等：已归档则跳过
            exists = await db.execute(
                select(ChatMessage.id).where(ChatMessage.stream_msg_id == stream_msg_id)
            )
            if exists.scalar_one_or_none() is not None:
                logger.debug("skip duplicate stream_msg_id=%s", stream_msg_id)
                return

            session: ChatSession | None = None
            if session_db_id_raw.isdigit():
                session = await db.get(ChatSession, int(session_db_id_raw))
            if session is None and session_public_id:
                result = await db.execute(
                    select(ChatSession).where(ChatSession.public_id == session_public_id)
                )
                session = result.scalar_one_or_none()
            if session is None:
                raise ValueError(f"session not found: {session_public_id}")

            public_id = str(message.get("id") or "")
            if public_id:
                dup_pub = await db.execute(
                    select(ChatMessage.id).where(ChatMessage.public_id == public_id)
                )
                if dup_pub.scalar_one_or_none() is not None:
                    # 同 public_id 已存在：补写 stream_msg_id 后返回
                    existing = (
                        await db.execute(
                            select(ChatMessage).where(ChatMessage.public_id == public_id)
                        )
                    ).scalar_one()
                    if not existing.stream_msg_id:
                        existing.stream_msg_id = stream_msg_id
                        await db.commit()
                    return

            created_ms = int(message.get("createdAt") or 0)
            from datetime import datetime, timezone

            created_at = (
                datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if created_ms > 0
                else datetime.now(timezone.utc)
            )

            row = ChatMessage(
                public_id=public_id or stream_msg_id.replace("-", "")[:32],
                session_id=session.id,
                role=str(message.get("role") or "user")[:16],
                content=str(message.get("content") or ""),
                mode=message.get("mode"),
                refs=message.get("refs") if isinstance(message.get("refs"), list) else None,
                stream_msg_id=stream_msg_id,
                model_name=message.get("modelName"),
                prompt_tokens=message.get("promptTokens"),
                completion_tokens=message.get("completionTokens"),
                total_tokens=message.get("totalTokens"),
                tool_name=message.get("toolName"),
                tool_input=message.get("toolInput"),
                tool_output=message.get("toolOutput"),
                tool_error=message.get("toolError"),
                tool_duration_ms=message.get("toolDurationMs"),
                created_at=created_at,
            )
            db.add(row)
            # 会话侧栏排序：以归档时间为准刷新（count 已由 API bump）
            session.last_message_at = created_at
            await db.commit()
            logger.debug(
                "archived stream_id=%s session=%s role=%s",
                stream_msg_id,
                session.public_id,
                row.role,
            )


async def run_consumer() -> None:
    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")
    consumer = ChatStreamConsumer()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("signal received, shutting down consumer…")
        consumer.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows 部分环境不支持 add_signal_handler
            signal.signal(sig, lambda *_: _signal_handler())

    try:
        await consumer.run_forever()
    finally:
        await close_redis()
