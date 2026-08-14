"""在独立事件循环中跑 stdio MCP，避免 cancel scope 泄漏到 FastAPI/ASGI 请求任务。

Windows + npx mssql-mcp-server 下，若在 ASGI 任务里打开 stdio session 再 abandon/aclose，
会在中间件退出时触发：
  RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's current cancel scope
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from api.services.mcp.client import McpClient, _content_to_text
from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)

# 挂在独立 loop 里放弃关闭的 stack，避免 GC 立刻在错误时机清理
_ORPHANS: list[Any] = []


def _build_client_from_snapshot(snap: dict[str, Any], *, timeout_seconds: float) -> McpClient:
    env = {str(k): str(v) for k, v in (snap.get("env") or {}).items()}
    default_cs = (env.get("MSSQL_CONNECTION_STRING") or "").strip()
    if default_cs:
        env.setdefault("CONNECTION_DEFAULT", default_cs)
        env.setdefault("CONNECTION_BESTMES", default_cs)
        env.setdefault("CONNECTION_BESTMESDB", default_cs)
    return McpClient(
        transport=str(snap.get("transport") or "stdio"),
        url=snap.get("url"),
        command=snap.get("command"),
        args=list(snap.get("args") or []),
        env=env,
        headers={str(k): str(v) for k, v in (snap.get("headers") or {}).items()},
        timeout_seconds=timeout_seconds,
    )


def snapshot_mcp_server(server: Any) -> dict[str, Any]:
    """把 ORM McpServer 打成可跨线程传递的纯 dict。"""
    env = server.env if isinstance(getattr(server, "env", None), dict) else {}
    headers = server.headers if isinstance(getattr(server, "headers", None), dict) else {}
    args = server.args if isinstance(getattr(server, "args", None), list) else []
    return {
        "publicId": getattr(server, "public_id", None),
        "name": getattr(server, "name", None),
        "transport": getattr(server, "transport", None),
        "url": getattr(server, "url", None),
        "command": getattr(server, "command", None),
        "args": list(args),
        "env": {str(k): str(v) for k, v in env.items()},
        "headers": {str(k): str(v) for k, v in headers.items()},
        "timeoutSeconds": float(getattr(server, "timeout_seconds", None) or 60),
    }


def run_execute_queries_isolated(
    *,
    server_snapshot: dict[str, Any],
    steps: list[tuple[str, str]],
    timeout_seconds: float = 90.0,
) -> dict[str, dict[str, Any]]:
    """同步入口：在全新事件循环里顺序执行多条 execute_query。

    供 asyncio.to_thread 调用，保证 stdio cancel scope 不进入 ASGI 任务。
    返回 {step_name: {"rows": ..., "raw": ..., "isError": bool}}
    """

    async def _main() -> dict[str, dict[str, Any]]:
        client = _build_client_from_snapshot(
            server_snapshot, timeout_seconds=timeout_seconds
        )
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            session = await stack.enter_async_context(client._session())
        except Exception:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass
            raise

        out: dict[str, dict[str, Any]] = {}
        try:
            for step, sql in steps:
                t0 = asyncio.get_running_loop().time()
                logger.warning(
                    "mcp.isolated step=%s start",
                    step,
                )
                try:
                    raw = await asyncio.wait_for(
                        session.call_tool(
                            "execute_query",
                            arguments={"query": sql},
                        ),
                        timeout=timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise AppError(
                        ErrorCode.INTERNAL,
                        f"MES 步骤超时（{step}，{timeout_seconds:.0f}s）",
                        status_code=504,
                    ) from exc
                is_error = bool(
                    getattr(raw, "is_error", False)
                    or getattr(raw, "isError", False)
                )
                content = _content_to_text(raw)
                if is_error:
                    raise AppError(
                        ErrorCode.INTERNAL,
                        f"MES 查询失败（{step}）：{content}",
                        status_code=502,
                    )
                elapsed = asyncio.get_running_loop().time() - t0
                logger.warning(
                    "mcp.isolated step=%s ok elapsed=%.1fs",
                    step,
                    elapsed,
                )
                # 先存 raw；结构化 rows 由调用方 _parse_mcp_rows 解析
                # （避免本模块循环依赖 casting.yield_analysis）
                out[step] = {
                    "rows": None,
                    "raw": content,
                    "content": content,
                    "isError": False,
                }
            return out
        finally:
            # 不在此 await aclose：会卡死；挂起 stack，错误清理只会落在本线程 loop 销毁阶段
            _ORPHANS.append(stack)
            if len(_ORPHANS) > 32:
                _ORPHANS.pop(0)
            logger.warning(
                "mcp.isolated abandon session orphans=%s",
                len(_ORPHANS),
            )

    # 降低独立 loop 销毁时的嘈杂日志（不影响主 ASGI；避免控制台出现 cancel scope ERROR）
    silenced = ("asyncio", "api.services.mcp.client")
    prev_levels = {name: logging.getLogger(name).level for name in silenced}
    for name in silenced:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        return asyncio.run(_main())
    finally:
        for name, level in prev_levels.items():
            logging.getLogger(name).setLevel(level)
