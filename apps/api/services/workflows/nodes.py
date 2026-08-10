"""工作流节点执行：按类型变更共享 state。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.agents import configs as agent_configs
from api.services.knowledge.ingest import search_chunks
from api.services.mcp import servers as mcp_servers
from api.services.models.runtime import build_llm_client
from api.services.prompts import configs as prompt_configs
from common.errors import AppError, ErrorCode
from db.models.mcp import McpServer, McpTool


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _format_chunks_context(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return ""
    parts: list[str] = []
    for i, c in enumerate(chunks[:8], start=1):
        score = float(c.get("score") or 0.0)
        content = str(c.get("content") or "").strip()
        name = str(c.get("name") or "").strip() or "未命名资料"
        parts.append(f"[#{i} 资料={name} 相似度={score:.3f}]\n{content}")
    return "\n\n---\n\n".join(parts)


def _replace_templates(value: Any, state: dict[str, Any]) -> Any:
    """简单 {{input}} / {{query}} / {{output}} 替换。"""
    mapping = {
        "{{input}}": _as_text(state.get("input")),
        "{{query}}": _as_text(state.get("query")),
        "{{output}}": _as_text(state.get("output")),
    }
    if isinstance(value, str):
        out = value
        for k, v in mapping.items():
            out = out.replace(k, v)
        return out
    if isinstance(value, dict):
        return {k: _replace_templates(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_templates(v, state) for v in value]
    return value


async def execute_node(
    db: AsyncSession,
    *,
    node: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """执行单节点并返回 detail 摘要（同时原地修改 state）。"""
    ntype = str(node.get("type") or "").strip()
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    if ntype == "start":
        return await _exec_start(state)
    if ntype == "knowledge":
        return await _exec_knowledge(db, data=data, state=state)
    if ntype == "llm":
        return await _exec_llm(db, data=data, state=state)
    if ntype == "agent":
        return await _exec_agent(db, data=data, state=state)
    if ntype == "mcp":
        return await _exec_mcp(db, data=data, state=state)
    if ntype == "end":
        return await _exec_end(state)
    raise AppError(
        ErrorCode.VALIDATION, f"unsupported node type: {ntype}", status_code=422
    )


async def _exec_start(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("input")
    vars_map: dict[str, Any] = dict(state.get("vars") or {})
    if isinstance(raw, dict):
        query = _as_text(raw.get("query") or raw.get("text") or "")
        ctx = raw.get("contextText") or raw.get("context")
        if ctx is not None and not _as_text(state.get("context")).strip():
            state["context"] = _as_text(ctx)
        if raw.get("instruction"):
            vars_map["instruction"] = raw.get("instruction")
        if raw.get("systemHint"):
            vars_map["systemHint"] = raw.get("systemHint")
    else:
        query = _as_text(raw)
    state["query"] = query
    state["vars"] = vars_map
    if not state.get("context"):
        state["context"] = ""
    return {"query": query, "hasContext": bool(_as_text(state.get("context")).strip())}


async def _exec_knowledge(
    db: AsyncSession, *, data: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    kb_ids_raw = data.get("knowledgeBaseIds") or data.get("knowledge_base_ids") or []
    kb_ids = [str(x).strip() for x in kb_ids_raw if str(x).strip()]
    top_k = int(data.get("topK") or data.get("top_k") or 5)
    top_k = max(1, min(top_k, 20))
    query = _as_text(state.get("query") or state.get("input")).strip()
    if not query:
        raise AppError(ErrorCode.VALIDATION, "knowledge node: empty query", status_code=422)
    if not kb_ids:
        raise AppError(
            ErrorCode.VALIDATION,
            "knowledge node: knowledgeBaseIds required",
            status_code=422,
        )
    chunks = await search_chunks(
        db, query=query, top_k=top_k, min_score=0.0, kb_ids=kb_ids
    )
    ctx = _format_chunks_context(chunks)
    prev = _as_text(state.get("context")).strip()
    state["context"] = f"{prev}\n\n{ctx}".strip() if prev else ctx
    state["refs"] = list(chunks)
    return {"query": query, "topK": top_k, "refs": len(chunks), "kbIds": kb_ids}


async def _exec_llm(
    db: AsyncSession, *, data: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    mode = str(data.get("mode") or "fast").strip().lower()
    if mode not in {"fast", "deep"}:
        mode = "fast"
    system_parts: list[str] = []
    prompt_id = data.get("promptId") or data.get("prompt_id")
    if prompt_id:
        content = await prompt_configs.get_enabled_content(db, str(prompt_id).strip())
        if content:
            system_parts.append(content)
    system_prompt = data.get("systemPrompt") or data.get("system_prompt")
    if system_prompt:
        system_parts.append(str(system_prompt))
    vars_map = state.get("vars") if isinstance(state.get("vars"), dict) else {}
    system_hint = vars_map.get("systemHint")
    if system_hint:
        system_parts.append(str(system_hint))
    context = _as_text(state.get("context")).strip()
    query = _as_text(state.get("query") or state.get("input")).strip()
    user_parts: list[str] = []
    if context:
        user_parts.append(f"【上下文】\n{context}")
    if query:
        user_parts.append(f"【问题】\n{query}")
    instruction = vars_map.get("instruction")
    if instruction:
        user_parts.append(f"【生成指令】\n{instruction}")
    if not user_parts:
        user_parts.append(_as_text(state.get("input")) or "请继续")
    messages: list[dict[str, str]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.append({"role": "user", "content": "\n\n".join(user_parts)})
    client = await build_llm_client(db, mode)
    text = await client.complete(messages, mode=mode)
    state["output"] = text
    return {"mode": mode, "chars": len(text or ""), "promptId": prompt_id}


async def _exec_agent(
    db: AsyncSession, *, data: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    agent_id = str(data.get("agentId") or data.get("agent_id") or "").strip()
    if not agent_id:
        raise AppError(ErrorCode.VALIDATION, "agent node: agentId required", status_code=422)
    bundle = await agent_configs.get_enabled_bundle(db, agent_id)
    mode = str(bundle.get("mode") or "fast")
    if mode not in {"fast", "deep"}:
        mode = "fast"
    query = _as_text(state.get("query") or state.get("input")).strip()
    kb_ids = list(bundle.get("knowledgeBaseIds") or [])
    refs: list[dict[str, Any]] = []
    if kb_ids and query:
        try:
            refs = await search_chunks(
                db, query=query, top_k=5, min_score=0.0, kb_ids=kb_ids
            )
        except Exception:  # noqa: BLE001
            refs = []
        if refs:
            ctx = _format_chunks_context(refs)
            prev = _as_text(state.get("context")).strip()
            state["context"] = f"{prev}\n\n{ctx}".strip() if prev else ctx
            state["refs"] = refs

    system_parts: list[str] = []
    prompt_id = bundle.get("promptId")
    if prompt_id:
        content = await prompt_configs.get_enabled_content(db, str(prompt_id))
        if content:
            system_parts.append(content)
    context = _as_text(state.get("context")).strip()
    user_parts: list[str] = []
    if context:
        user_parts.append(f"【上下文】\n{context}")
    if query:
        user_parts.append(f"【问题】\n{query}")
    if not user_parts:
        user_parts.append(_as_text(state.get("input")) or "请继续")
    messages: list[dict[str, str]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.append({"role": "user", "content": "\n\n".join(user_parts)})
    client = await build_llm_client(db, mode)
    text = await client.complete(messages, mode=mode)
    state["output"] = text
    return {
        "agentId": agent_id,
        "agentName": bundle.get("name"),
        "mode": mode,
        "refs": len(refs),
        "chars": len(text or ""),
    }


async def _resolve_mcp_target(
    db: AsyncSession, data: dict[str, Any]
) -> tuple[str, str]:
    """返回 (server_public_id, tool_name)。"""
    tool_id = str(data.get("toolId") or data.get("tool_id") or "").strip()
    if tool_id:
        result = await db.execute(
            select(McpTool)
            .where(McpTool.public_id == tool_id)
            .options(selectinload(McpTool.server))
        )
        tool = result.scalar_one_or_none()
        if tool is None:
            raise AppError(ErrorCode.NOT_FOUND, "mcp tool not found", status_code=404)
        server = tool.server
        if server is None:
            # fallback load
            server = await db.get(McpServer, tool.server_id)
        if server is None:
            raise AppError(ErrorCode.NOT_FOUND, "mcp server not found", status_code=404)
        return server.public_id, tool.name

    server_id = str(data.get("serverId") or data.get("server_id") or "").strip()
    tool_name = str(data.get("toolName") or data.get("tool_name") or "").strip()
    if not server_id or not tool_name:
        raise AppError(
            ErrorCode.VALIDATION,
            "mcp node: toolId or (serverId+toolName) required",
            status_code=422,
        )
    return server_id, tool_name


async def _exec_mcp(
    db: AsyncSession, *, data: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    server_id, tool_name = await _resolve_mcp_target(db, data)
    raw_args = data.get("arguments")
    if not isinstance(raw_args, dict):
        raw_args = {}
    arguments = _replace_templates(raw_args, state)
    if not isinstance(arguments, dict):
        arguments = {}
    result = await mcp_servers.call_tool_on_server(
        db,
        server_public_id=server_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    state["lastToolResult"] = result
    text = _as_text(result.get("content") if isinstance(result, dict) else result)
    prev_ctx = _as_text(state.get("context")).strip()
    tool_block = f"【MCP {tool_name}】\n{text}"
    state["context"] = f"{prev_ctx}\n\n{tool_block}".strip() if prev_ctx else tool_block
    prev_out = _as_text(state.get("output")).strip()
    state["output"] = f"{prev_out}\n\n{text}".strip() if prev_out else text
    return {
        "serverId": server_id,
        "toolName": tool_name,
        "isError": bool(result.get("isError")) if isinstance(result, dict) else False,
        "preview": text[:500],
    }


async def _exec_end(state: dict[str, Any]) -> dict[str, Any]:
    out = state.get("output")
    if out is None or (isinstance(out, str) and not out.strip()):
        # 兜底：用 query / lastToolResult / context
        if state.get("lastToolResult") is not None:
            state["output"] = state.get("lastToolResult")
        elif _as_text(state.get("query")).strip():
            state["output"] = state.get("query")
        else:
            state["output"] = state.get("context") or state.get("input")
    return {"outputPreview": _as_text(state.get("output"))[:500]}
