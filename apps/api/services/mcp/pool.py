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

# Windows 下 stdio aclose 会阻塞事件循环；若直接丢弃 stack 又会被 GC 在错误 Task 里
# 触发 cancel scope 异常并取消正在返回的 HTTP 请求。因此把 stack 挂到进程级列表，
# 故意泄漏到进程退出，优先保证 HTTP 能返回。
_ORPHAN_STACKS: list[Any] = []


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
            logger.info(
                "mcp pool open session server=%s transport=%s",
                self.server_id,
                self.client.transport,
            )
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
            await self.abandon(reason=f"call_tool_timeout:{name}")
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP call_tool 超时（{timeout:.0f}s）：{name}",
                status_code=502,
            ) from exc
        except Exception:
            await self.abandon(reason=f"call_tool_error:{name}")
            raise

        is_error = bool(getattr(result, "isError", None) or getattr(result, "is_error", False))
        return McpCallResult(
            content=_content_to_text(result),
            raw=result.model_dump() if hasattr(result, "model_dump") else result,
            is_error=is_error,
        )

    async def abandon(self, *, reason: str = "") -> None:
        """结束池化会话但不调用会卡死的 aclose。

        将 AsyncExitStack 挂到模块级列表，避免 GC 在错误任务中退出 cancel scope，
        从而取消已经算完的 HTTP 请求（前端表现为「查询超时」）。
        """
        async with self._lock:
            stack = self._stack
            self._closed = True
            self._session = None
            self._stack = None
        if stack is not None:
            _ORPHAN_STACKS.append(stack)
            # 防止列表无限增长（进程内多次分析）
            if len(_ORPHAN_STACKS) > 32:
                _ORPHAN_STACKS.pop(0)
            logger.warning(
                "mcp pool abandoned server=%s reason=%s orphans=%s "
                "(stdio child kept until API process exit)",
                self.server_id,
                reason or "close",
                len(_ORPHAN_STACKS),
            )

    async def close(self) -> None:
        await self.abandon(reason="close")


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
            await entry.abandon(reason="pool_aclose")
        if entries:
            logger.warning("mcp pool abandoned %s session(s)", len(entries))
