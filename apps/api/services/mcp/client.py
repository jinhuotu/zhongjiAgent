"""MCP Client：基于官方 mcp SDK（stdio / SSE / streamable HTTP）。

youqiAgent 仅有 MCP 占位，无实现可参考；本模块用官方协议栈替换自研 JSON-RPC，
并保留 Windows 下 node/npx 启动修复。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from common.errors import AppError, ErrorCode
from common.logging import get_logger

if TYPE_CHECKING:
    from mcp import ClientSession

logger = get_logger(__name__)


@dataclass
class McpToolDef:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpCallResult:
    content: str
    raw: Any = None
    is_error: bool = False


def _content_to_text(result: Any) -> str:
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), ensure_ascii=False)
    return str(result)


def _ensure_node_path(env: dict[str, str]) -> dict[str, str]:
    if sys.platform != "win32":
        return env
    extras = [
        r"C:\Program Files\nodejs",
        r"C:\Program Files (x86)\nodejs",
        os.path.expandvars(r"%APPDATA%\npm"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\node"),
        os.path.expandvars(r"%APPDATA%\nvm"),
    ]
    # nvm 当前版本目录
    nvm_home = os.environ.get("NVM_HOME") or os.path.expandvars(r"%APPDATA%\nvm")
    nvm_symlink = os.environ.get("NVM_SYMLINK")
    for p in (nvm_home, nvm_symlink):
        if p and os.path.isdir(p):
            extras.append(p)
    path_key = "Path" if "Path" in env and "PATH" not in env else "PATH"
    cur = env.get(path_key) or env.get("PATH") or env.get("Path") or ""
    parts = [p for p in cur.split(";") if p]
    for p in extras:
        if p and os.path.isdir(p) and p not in parts:
            parts.insert(0, p)
    env[path_key] = ";".join(parts)
    env["PATH"] = env[path_key]
    return env


def _find_node() -> str | None:
    return shutil.which("node.exe") or shutil.which("node")


def _mssql_mcp_entry_js() -> Path | None:
    """定位全局 mssql-mcp-server 的 dist/index.js。"""
    node = _find_node()
    candidates: list[Path] = []
    if node:
        node_dir = Path(node).resolve().parent
        candidates.append(node_dir / "node_modules" / "mssql-mcp-server" / "dist" / "index.js")
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_key)
        if not base:
            continue
        candidates.append(
            Path(base) / "npm" / "node_modules" / "mssql-mcp-server" / "dist" / "index.js"
        )
        candidates.append(
            Path(base) / "nvm" / "v20.18.0" / "node_modules" / "mssql-mcp-server" / "dist" / "index.js"
        )
    # nvm 当前版本：扫描 nvm 目录下常见路径
    nvm_home = os.environ.get("NVM_HOME") or os.path.expandvars(r"%APPDATA%\nvm")
    if nvm_home and os.path.isdir(nvm_home):
        try:
            for child in Path(nvm_home).iterdir():
                if child.is_dir() and child.name.startswith("v"):
                    candidates.append(
                        child / "node_modules" / "mssql-mcp-server" / "dist" / "index.js"
                    )
        except OSError:
            pass
    for p in candidates:
        if p.is_file():
            return p
    return None


def _resolve_stdio_command_args(command: str, args: list[str]) -> tuple[str, list[str]]:
    """解析 stdio 启动命令。

    - Windows：避免 .cmd/.ps1 经管道秒退
    - npx -y mssql-mcp-server：优先改为 node + 全局包入口（快很多）
    - command=mssql-mcp-server：同样直连 dist/index.js
    """
    raw = (command or "").strip()
    base = os.path.basename(raw).lower()
    arg_list = [str(a) for a in args]
    arg_l = [a.lower() for a in arg_list]

    # 已全局安装时：绕过 npx 冷启动
    wants_mssql = base in {"mssql-mcp-server", "mssql-mcp-server.cmd", "mssql-mcp-server.ps1"} or (
        base in {"npx", "npx.cmd"} and any("mssql-mcp-server" in a for a in arg_l)
    )
    if wants_mssql:
        entry = _mssql_mcp_entry_js()
        node = _find_node()
        if entry and node:
            logger.warning("mcp stdio using global mssql entry: %s %s", node, entry)
            # 去掉 -y / 包名，保留其余参数
            extra = [
                a
                for a in arg_list
                if a.lower() not in {"-y", "--yes", "mssql-mcp-server"}
            ]
            return str(node), [str(entry), *extra]

    if base in {"npx", "npx.cmd"}:
        node = _find_node()
        if node:
            node_dir = Path(node).resolve().parent
            for rel in (
                ("node_modules", "npx", "bin", "npx-cli.js"),
                ("node_modules", "npm", "bin", "npx-cli.js"),
            ):
                cli = node_dir.joinpath(*rel)
                if cli.is_file():
                    logger.warning("mcp stdio using node+npx-cli: %s %s", node, cli)
                    return str(node), [str(cli), *arg_list]
        found = shutil.which("npx.cmd") or shutil.which("npx")
        if found:
            return found, arg_list

    if os.path.isfile(raw):
        return raw, arg_list
    found = shutil.which(raw)
    if sys.platform == "win32" and not found:
        for suffix in (".cmd", ".exe", ".bat"):
            found = shutil.which(raw + suffix)
            if found:
                break
    return (found or raw), arg_list


class McpClient:
    """官方 MCP SDK 封装，对外 API 与管理端 / 对话运行时保持兼容。"""

    def __init__(
        self,
        *,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.transport = (transport or "").strip().lower()
        self.url = (url or "").strip()
        self.command = (command or "").strip()
        self.args = [str(x) for x in (args or [])]
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.headers = {str(k): str(v) for k, v in (headers or {}).items() if str(k).strip()}
        self.timeout_seconds = float(timeout_seconds or 60.0)

        if self.transport not in ("streamable_http", "sse", "stdio"):
            raise AppError(
                ErrorCode.VALIDATION,
                "transport must be streamable_http, sse or stdio",
                status_code=422,
            )
        if self.transport in ("streamable_http", "sse") and not self.url:
            raise AppError(
                ErrorCode.VALIDATION,
                "url required for http/sse transport",
                status_code=422,
            )
        if self.transport == "stdio" and not self.command:
            raise AppError(
                ErrorCode.VALIDATION,
                "command required for stdio transport",
                status_code=422,
            )

    async def list_tools(self) -> list[McpToolDef]:
        async def _run() -> Any:
            async with self._session() as session:
                return await session.list_tools()

        try:
            result = await self._run_cancellable(_run(), label="list_tools")
        except TimeoutError as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP list_tools 超时（{self.timeout_seconds:.0f}s）。"
                "stdio+npx 首次拉包较慢，可调大超时或改为全局安装 mssql-mcp-server。",
                status_code=502,
            ) from exc
        except AppError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp list_tools failed")
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP list_tools 失败：{exc}",
                status_code=502,
            ) from exc

        items: list[McpToolDef] = []
        for t in getattr(result, "tools", None) or []:
            name = str(getattr(t, "name", "") or "").strip()
            if not name:
                continue
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            if not isinstance(schema, dict):
                schema = {}
            items.append(
                McpToolDef(
                    name=name,
                    description=str(getattr(t, "description", "") or ""),
                    input_schema=schema,
                )
            )
        return items

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpCallResult:
        async def _run() -> Any:
            async with self._session() as session:
                return await session.call_tool(name, arguments=arguments or {})

        try:
            result = await self._run_cancellable(_run(), label=f"call_tool:{name}")
        except TimeoutError as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP call_tool 超时（{self.timeout_seconds:.0f}s）：{name}",
                status_code=502,
            ) from exc
        except AppError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp call_tool failed name=%s", name)
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP call_tool 失败：{exc}",
                status_code=502,
            ) from exc

        is_error = bool(getattr(result, "isError", None) or getattr(result, "is_error", False))
        return McpCallResult(
            content=_content_to_text(result),
            raw=result.model_dump() if hasattr(result, "model_dump") else result,
            is_error=is_error,
        )

    async def _run_cancellable(self, coro: Any, *, label: str) -> Any:
        """带超时的可取消执行；取消/超时时尽快放弃清理，避免拖住会话锁与 worker。"""
        task = asyncio.create_task(coro, name=f"mcp:{label}")
        try:
            return await asyncio.wait_for(task, timeout=self.timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.5)
                except Exception:  # noqa: BLE001
                    logger.warning("mcp %s cleanup abandoned after cancel/timeout", label)
            raise

    async def health_check(self) -> dict[str, Any]:
        tools = await self.list_tools()
        return {
            "ok": True,
            "transport": self.transport,
            "toolCount": len(tools),
            "tools": [{"name": t.name, "description": t.description} for t in tools],
        }

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        if self.transport == "stdio":
            async with self._stdio_session() as session:
                yield session
        elif self.transport == "sse":
            async with self._sse_session() as session:
                yield session
        else:
            async with self._http_session() as session:
                yield session

    @asynccontextmanager
    async def _stdio_session(self) -> AsyncIterator[ClientSession]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cmd, args = _resolve_stdio_command_args(self.command, self.args)
        env = {str(k): str(v) for k, v in os.environ.items()}
        env.update(self.env)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("FORCE_COLOR", "0")
        env = _ensure_node_path(env)

        logger.warning(
            "mcp stdio connect command=%s args=%s envKeys=%s",
            cmd,
            args,
            sorted(self.env.keys()),
        )
        params = StdioServerParameters(
            command=cmd,
            args=args,
            env=env,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                    yield session
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp stdio session failed")
            raise AppError(
                ErrorCode.INTERNAL,
                "MCP stdio 连接失败："
                f"{exc}。命令：{cmd} {' '.join(args)}。"
                "请确认 Node.js/npx 可用；"
                "或 npm i -g mssql-mcp-server 后改用 command=mssql-mcp-server。",
                status_code=502,
            ) from exc

    @asynccontextmanager
    async def _sse_session(self) -> AsyncIterator[ClientSession]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with sse_client(
                self.url,
                headers=self.headers or None,
                timeout=min(30.0, self.timeout_seconds),
                sse_read_timeout=self.timeout_seconds,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                    yield session
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp sse session failed")
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP SSE 连接失败：{exc}",
                status_code=502,
            ) from exc

    @asynccontextmanager
    async def _http_session(self) -> AsyncIterator[ClientSession]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with streamablehttp_client(
                self.url,
                headers=self.headers or None,
                timeout=self.timeout_seconds,
                sse_read_timeout=self.timeout_seconds,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                    yield session
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp streamable_http session failed")
            raise AppError(
                ErrorCode.INTERNAL,
                f"MCP Streamable HTTP 连接失败：{exc}",
                status_code=502,
            ) from exc


def openai_tool_name(server_public_id: str, tool_name: str) -> str:
    raw = f"mcp_{server_public_id}_{tool_name}"
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return cleaned[:64]


def parse_openai_tool_name(fn_name: str) -> tuple[str, str] | None:
    if not fn_name.startswith("mcp_"):
        return None
    rest = fn_name[4:]
    parts = rest.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


async def iter_chunked_text(text: str, size: int = 24) -> AsyncIterator[str]:
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i : i + size]
        await asyncio.sleep(0)
