from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.ai import memory as memory_svc
from api.services.ai.llm import LLMClient
from api.services.mcp import servers as mcp_svc
from api.services.mcp.client import iter_chunked_text, openai_tool_name, parse_openai_tool_name
from api.services.mcp.pool import McpSessionPool
from common.logging import get_logger
from common.redis_tools import check_sliding_rate_limit

logger = get_logger(__name__)

MAX_TOOL_ROUNDS = 5


def to_openai_tools(enabled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in enabled:
        schema = item.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        fn_name = openai_tool_name(str(item["serverId"]), str(item["name"]))
        if fn_name in seen_names:
            logger.warning(
                "skip duplicate openai tool name=%s server=%s tool=%s",
                fn_name,
                item.get("serverName"),
                item.get("name"),
            )
            continue
        seen_names.add(fn_name)
        name = str(item.get("name") or "")
        desc = item.get("description") or f"MCP tool {name} from {item.get('serverName')}"
        # 引导模型走默认可达路径，避免 connectionName=default / 盲目 list_databases
        hints = {
            "list_connections": " Only reports env config; does not prove SQL login works.",
            "test_connection": " Prefer omitting connectionName to use MSSQL_CONNECTION_STRING.",
            "list_databases": " Prefer list_tables on the default DB instead; list_databases is slow and often times out.",
            "list_tables": " Prefer omitting connectionName. Use this before sample_data/execute_query.",
            "sample_data": " Prefer omitting connectionName.",
            "execute_query": " Prefer omitting connectionName. Use for SELECT only.",
            "describe_table": " Prefer omitting connectionName.",
        }
        if name in hints:
            desc = f"{desc}{hints[name]}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": fn_name,
                    "description": str(desc)[:1024],
                    "parameters": schema,
                },
            }
        )
    return tools


def _filter_enabled_tools(
    enabled: list[dict[str, Any]],
    *,
    tools_enabled: bool = True,
    allowed_tool_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """tools_enabled=False → 无工具；allowed_tool_ids 非空 → 白名单；None/空列表且启用 → 不额外限制。"""
    if not tools_enabled:
        return []
    if allowed_tool_ids:
        allow = {str(x).strip() for x in allowed_tool_ids if str(x).strip()}
        if allow:
            return [t for t in enabled if str(t.get("toolId") or "") in allow]
    return enabled


async def run_chat_with_mcp(
    db: AsyncSession,
    *,
    client: LLMClient,
    llm_messages: list[dict[str, Any]],
    mode: str,
    session: Any,
    user_id: int,
    kb_ids: list[str],
    chunks: list[dict[str, Any]],  # noqa: ARG001 — 预留 refs 透传
    tools_enabled: bool = True,
    allowed_tool_ids: list[str] | None = None,
) -> AsyncIterator[dict[str, str]]:
    """带 MCP 工具循环的对话生成器，产出 SSE event dict。

    同一轮对话内通过 McpSessionPool 复用 MCP 进程/会话，避免每个 tool 冷启动。
    allowed_tool_ids：非空时按 mcp_tools.public_id 白名单过滤；空/None 且 tools_enabled
    为 True 时使用全部已启用工具。
    """
    raw_enabled = await mcp_svc.list_enabled_tools_for_chat(db)
    enabled = _filter_enabled_tools(
        raw_enabled,
        tools_enabled=tools_enabled,
        allowed_tool_ids=allowed_tool_ids,
    )
    tools = to_openai_tools(enabled)
    allowed_openai_names = {t["function"]["name"] for t in tools}
    working: list[dict[str, Any]] = [dict(m) for m in llm_messages]

    if not tools:
        logger.info(
            "chat mcp: no tools (enabled=%s whitelist=%s), plain stream mode=%s",
            tools_enabled,
            bool(allowed_tool_ids),
            mode,
        )
        accumulated = ""
        async for text in client.stream_chat(
            [{"role": m["role"], "content": str(m.get("content") or "")} for m in working],
            mode=mode,
        ):
            accumulated += text
            yield {
                "event": "delta",
                "data": json.dumps({"text": text}, ensure_ascii=False),
            }
        yield {
            "event": "__final__",
            "data": json.dumps({"content": accumulated}, ensure_ascii=False),
        }
        return

    logger.info(
        "chat mcp: %s enabled tools, mode=%s names=%s",
        len(tools),
        mode,
        [t["function"]["name"] for t in tools[:8]],
    )

    pool = McpSessionPool()
    try:
        async for ev in _run_tool_loop(
            db,
            client=client,
            tools=tools,
            working=working,
            mode=mode,
            session=session,
            user_id=user_id,
            kb_ids=kb_ids,
            pool=pool,
            allowed_openai_names=allowed_openai_names,
        ):
            yield ev
    finally:
        await pool.aclose()


async def _run_tool_loop(
    db: AsyncSession,
    *,
    client: LLMClient,
    tools: list[dict[str, Any]],
    working: list[dict[str, Any]],
    mode: str,
    session: Any,
    user_id: int,
    kb_ids: list[str],
    pool: McpSessionPool,
    allowed_openai_names: set[str] | None = None,
) -> AsyncIterator[dict[str, str]]:
    accumulated = ""

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            result = await client.complete_message(working, mode=mode, tools=tools)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat mcp LLM complete_message failed")
            msg = str(getattr(exc, "msg", None) or exc)
            yield {
                "event": "error",
                "data": json.dumps({"msg": f"模型调用失败：{msg}"}, ensure_ascii=False),
            }
            yield {
                "event": "__final__",
                "data": json.dumps({"content": accumulated}, ensure_ascii=False),
            }
            return

        tool_calls = result.get("tool_calls") or []
        content = str(result.get("content") or "")

        # 工具调用前的自然语言说明先推给前端（已剥离 DSML）
        if tool_calls and content.strip():
            async for text in iter_chunked_text(content.strip()):
                accumulated += text
                yield {
                    "event": "delta",
                    "data": json.dumps({"text": text}, ensure_ascii=False),
                }
            accumulated += "\n\n"

        if not tool_calls:
            if content:
                async for text in iter_chunked_text(content):
                    accumulated += text
                    yield {
                        "event": "delta",
                        "data": json.dumps({"text": text}, ensure_ascii=False),
                    }
            else:
                async for text in client.stream_chat(
                    [{"role": m["role"], "content": str(m.get("content") or "")} for m in working],
                    mode=mode,
                ):
                    accumulated += text
                    yield {
                        "event": "delta",
                        "data": json.dumps({"text": text}, ensure_ascii=False),
                    }
            yield {
                "event": "__final__",
                "data": json.dumps({"content": accumulated}, ensure_ascii=False),
            }
            return

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc.get("arguments") or "{}",
                    },
                }
                for tc in tool_calls
            ],
        }
        working.append(assistant_msg)

        for tc in tool_calls:
            await check_sliding_rate_limit(scope="mcp", subject=str(user_id), max_requests=60)
            fn_name = str(tc.get("name") or "")
            parsed = parse_openai_tool_name(fn_name)
            args_raw = tc.get("arguments") or "{}"
            try:
                arguments = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}

            yield {
                "event": "tool",
                "data": json.dumps(
                    {
                        "phase": "call",
                        "toolCallId": tc.get("id"),
                        "name": fn_name,
                        "serverId": parsed[0] if parsed else None,
                        "toolName": parsed[1] if parsed else fn_name,
                        "arguments": arguments,
                    },
                    ensure_ascii=False,
                ),
            }

            t0 = time.perf_counter()
            tool_error: str | None = None
            tool_content = ""
            if allowed_openai_names is not None and fn_name not in allowed_openai_names:
                tool_error = f"tool not allowed for this agent: {fn_name}"
                tool_content = tool_error
            elif not parsed:
                tool_error = f"unknown tool mapping: {fn_name}"
                tool_content = tool_error
            else:
                server_id, tool_name = parsed
                try:
                    call_res = await mcp_svc.call_tool_on_server(
                        db,
                        server_public_id=server_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        pool=pool,
                    )
                    tool_content = str(call_res.get("content") or "")
                    if call_res.get("isError"):
                        tool_error = tool_content
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mcp tool call failed: %s", exc)
                    tool_error = str(getattr(exc, "msg", None) or exc)
                    tool_content = f"tool error: {tool_error}"

            duration_ms = int((time.perf_counter() - t0) * 1000)
            display_name = parsed[1] if parsed else fn_name
            logger.info(
                "chat mcp tool done name=%s err=%s durationMs=%s pooled=1",
                display_name,
                bool(tool_error),
                duration_ms,
            )

            tool_hot = memory_svc.build_hot_message(
                role="tool",
                content=tool_content,
                mode=mode,
                knowledge_base_ids=kb_ids,
                tool_name=display_name,
                tool_input=arguments,
                tool_output={"text": tool_content} if not tool_error else None,
                tool_error=tool_error,
                tool_duration_ms=duration_ms,
            )
            await memory_svc.append_hot_and_enqueue(session=session, message=tool_hot)

            yield {
                "event": "tool",
                "data": json.dumps(
                    {
                        "phase": "result",
                        "toolCallId": tc.get("id"),
                        "name": fn_name,
                        "toolName": display_name,
                        "serverId": parsed[0] if parsed else None,
                        "content": tool_content[:4000],
                        "error": tool_error,
                        "durationMs": duration_ms,
                    },
                    ensure_ascii=False,
                ),
            }

            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": tool_content,
                }
            )

    async for text in client.stream_chat(working, mode=mode):
        accumulated += text
        yield {
            "event": "delta",
            "data": json.dumps({"text": text}, ensure_ascii=False),
        }
    yield {
        "event": "__final__",
        "data": json.dumps({"content": accumulated}, ensure_ascii=False),
    }
