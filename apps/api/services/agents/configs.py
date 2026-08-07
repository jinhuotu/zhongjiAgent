from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.agent import ScenarioAgent
from db.models.knowledge import KnowledgeBase
from db.models.mcp import McpTool
from db.models.prompt import Prompt

_ALLOWED_MODES = frozenset({"fast", "deep"})
_UNSET = object()


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def _normalize_id_list(raw: list[str] | None, *, max_len: int) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        pid = (item or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        if len(out) >= max_len:
            break
    return out


def to_item(row: ScenarioAgent) -> dict[str, Any]:
    kb_ids = row.knowledge_base_ids if isinstance(row.knowledge_base_ids, list) else []
    tool_ids = row.mcp_tool_ids if isinstance(row.mcp_tool_ids, list) else []
    return {
        "id": row.public_id,
        "name": row.name,
        "remark": row.remark,
        "enabled": bool(row.enabled),
        "promptId": row.prompt_public_id,
        "knowledgeBaseIds": [str(x) for x in kb_ids if x],
        "mode": row.mode if row.mode in _ALLOWED_MODES else "fast",
        "mcpToolIds": [str(x) for x in tool_ids if x],
        "toolsEnabled": bool(row.tools_enabled),
        "createdAt": int(row.created_at.timestamp() * 1000) if row.created_at else 0,
        "updatedAt": int(row.updated_at.timestamp() * 1000) if row.updated_at else 0,
    }


async def _validate_prompt_id(db: AsyncSession, prompt_id: str | None) -> str | None:
    if prompt_id is None:
        return None
    pid = prompt_id.strip()
    if not pid:
        return None
    result = await db.execute(select(Prompt.id).where(Prompt.public_id == pid))
    if result.scalar_one_or_none() is None:
        raise AppError(ErrorCode.VALIDATION, "prompt not found", status_code=422)
    return pid


async def _validate_knowledge_base_ids(
    db: AsyncSession, kb_ids: list[str]
) -> list[str]:
    normalized = _normalize_id_list(kb_ids, max_len=32)
    if not normalized:
        return []
    result = await db.execute(
        select(KnowledgeBase.public_id).where(KnowledgeBase.public_id.in_(normalized))
    )
    found = {str(x) for x in result.scalars().all()}
    missing = [x for x in normalized if x not in found]
    if missing:
        raise AppError(
            ErrorCode.VALIDATION,
            f"knowledge base not found: {', '.join(missing[:5])}",
            status_code=422,
        )
    return normalized


async def _validate_mcp_tool_ids(db: AsyncSession, tool_ids: list[str]) -> list[str]:
    normalized = _normalize_id_list(tool_ids, max_len=64)
    if not normalized:
        return []
    result = await db.execute(
        select(McpTool.public_id).where(McpTool.public_id.in_(normalized))
    )
    found = {str(x) for x in result.scalars().all()}
    missing = [x for x in normalized if x not in found]
    if missing:
        raise AppError(
            ErrorCode.VALIDATION,
            f"mcp tool not found: {', '.join(missing[:5])}",
            status_code=422,
        )
    return normalized


async def get_by_public_id(db: AsyncSession, public_id: str) -> ScenarioAgent:
    result = await db.execute(
        select(ScenarioAgent).where(ScenarioAgent.public_id == public_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "agent not found", status_code=404)
    return row


async def list_configs(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ScenarioAgent).order_by(ScenarioAgent.updated_at.desc())
    )
    return [to_item(r) for r in result.scalars().all()]


async def list_options(db: AsyncSession) -> list[dict[str, Any]]:
    """对话/选择器用：仅启用项，含完整配置字段。"""
    result = await db.execute(
        select(ScenarioAgent)
        .where(ScenarioAgent.enabled.is_(True))
        .order_by(ScenarioAgent.updated_at.desc())
    )
    return [to_item(r) for r in result.scalars().all()]


async def get_enabled_bundle(db: AsyncSession, public_id: str) -> dict[str, Any]:
    """阶段二对话解析用：启用智能体的配置包。"""
    row = await get_by_public_id(db, public_id)
    if not row.enabled:
        raise AppError(ErrorCode.VALIDATION, "agent is disabled", status_code=422)
    return to_item(row)


async def create_config(
    db: AsyncSession,
    *,
    name: str,
    remark: str | None = None,
    enabled: bool = True,
    prompt_id: str | None = None,
    knowledge_base_ids: list[str] | None = None,
    mode: str = "fast",
    mcp_tool_ids: list[str] | None = None,
    tools_enabled: bool = True,
    created_by: int | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
    mode_norm = (mode or "fast").strip().lower()
    if mode_norm not in _ALLOWED_MODES:
        raise AppError(ErrorCode.VALIDATION, "mode must be fast or deep", status_code=422)

    prompt_public_id = await _validate_prompt_id(db, prompt_id)
    kb_ids = await _validate_knowledge_base_ids(db, knowledge_base_ids or [])
    tool_ids = await _validate_mcp_tool_ids(db, mcp_tool_ids or [])

    row = ScenarioAgent(
        public_id=short_id(12),
        name=name.strip()[:128],
        remark=(remark.strip()[:512] if remark and remark.strip() else None),
        enabled=bool(enabled),
        prompt_public_id=prompt_public_id,
        knowledge_base_ids=kb_ids,
        mode=mode_norm,
        mcp_tool_ids=tool_ids,
        tools_enabled=bool(tools_enabled),
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return to_item(row)


async def update_config(
    db: AsyncSession,
    *,
    public_id: str,
    name: str | None = None,
    remark: str | None = None,
    enabled: bool | None = None,
    prompt_id: Any = _UNSET,
    knowledge_base_ids: list[str] | None = None,
    mode: str | None = None,
    mcp_tool_ids: list[str] | None = None,
    tools_enabled: bool | None = None,
) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
        row.name = name.strip()[:128]
    if remark is not None:
        row.remark = remark.strip()[:512] if remark.strip() else None
    if enabled is not None:
        row.enabled = bool(enabled)
    if prompt_id is not _UNSET:
        row.prompt_public_id = await _validate_prompt_id(
            db, prompt_id if isinstance(prompt_id, str) else None
        )
    if knowledge_base_ids is not None:
        row.knowledge_base_ids = await _validate_knowledge_base_ids(
            db, knowledge_base_ids
        )
    if mode is not None:
        mode_norm = mode.strip().lower()
        if mode_norm not in _ALLOWED_MODES:
            raise AppError(
                ErrorCode.VALIDATION, "mode must be fast or deep", status_code=422
            )
        row.mode = mode_norm
    if mcp_tool_ids is not None:
        row.mcp_tool_ids = await _validate_mcp_tool_ids(db, mcp_tool_ids)
    if tools_enabled is not None:
        row.tools_enabled = bool(tools_enabled)
    await db.commit()
    await db.refresh(row)
    return to_item(row)


async def delete_config(db: AsyncSession, *, public_id: str) -> None:
    row = await get_by_public_id(db, public_id)
    await db.delete(row)
    await db.commit()
