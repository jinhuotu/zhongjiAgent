"""对话轮次内 MCP 会话复用：同一 server 只拉起一次进程，多次 call_tool 共用连接。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from api.services.mcp.client import McpCallResult, McpClient, _content_to_text
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from db.models.mcp import McpServer

logger = get_logger(__name__)


class _PooledEntry:
    def __init__(self, client: McpClient, *, server_id: str) -> None:
        self.client = client
        self.server_id = server_id
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def ensure_session(self) -> Any:
        async with self._lock:
            if self._closed:
                raise AppError(
                    ErrorCode.INTERNAL,
                    f"MCP pooled session already closed: {self.server_id}",
                    status_code=502,
                )
            if self._session is not None:
                return self._session
            logger.info("mcp pool open session server=%s transport=%s", self.server_id, self.client.transport)
            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                session = await stack.enter_async_context(self.client._session())
            except Exception:
                try:
                    await stack.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._stack = stack
            self._session = session
            return session

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        session = await self.ensure_session()
        timeout = float(self.client.timeout_seconds or 60.0)
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments=arguments or {}),
                timeout=timeout,
            )
        except TimeoutError as exc:
            # 超时后会话可能半死，丢弃以便下次重建
            await self.close()
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP call_tool 超时（{timeout:.0f}s）：{name}",
                status_code=502,
            ) from exc
        except Exception:
            await self.close()
            raise

        is_error = bool(getattr(result, "isError", None) or getattr(result, "is_error", False))
        return McpCallResult(
            content=_content_to_text(result),
            raw=result.model_dump() if hasattr(result, "model_dump") else result,
            is_error=is_error,
        )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._session = None
            stack = self._stack
            self._stack = None
            if stack is None:
                return
            try:
                await asyncio.wait_for(stack.aclose(), timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mcp pool close abandoned server=%s: %s", self.server_id, exc)


class McpSessionPool:
    """按 MCP server public_id 复用已打开的 ClientSession（含 stdio 子进程）。"""

    def __init__(self) -> None:
        self._entries: dict[str, _PooledEntry] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, server: McpServer, client: McpClient) -> _PooledEntry:
        sid = server.public_id
        async with self._lock:
            entry = self._entries.get(sid)
            if entry is not None and not entry._closed:
                return entry
            entry = _PooledEntry(client, server_id=sid)
            self._entries[sid] = entry
            return entry

    async def call_tool(
        self,
        server: McpServer,
        client: McpClient,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> McpCallResult:
        entry = await self.get_or_create(server, client)
        try:
            return await entry.call_tool(tool_name, arguments)
        except Exception:
            # 失败条目剔除，避免脏会话被继续复用
            async with self._lock:
                cur = self._entries.get(server.public_id)
                if cur is entry:
                    self._entries.pop(server.public_id, None)
            raise

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await entry.close()
        if entries:
            logger.info("mcp pool closed %s session(s)", len(entries))
