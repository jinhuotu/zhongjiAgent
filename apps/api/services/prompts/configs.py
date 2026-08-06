from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.prompt import Prompt

# 与历史热配置默认文案一致，空表时种子一条便于选择
_DEFAULT_SEED_NAME = "窑炉领域助手"
_DEFAULT_SEED_CONTENT = """你是工业燃气车式窑（车底炉）领域助手，可结合企业知识库中的操作手册、规程、应急预案与标准条文作答。

【角色定位】
- 服务现场：设备运维、安全操作与禁令、突发应急、燃烧控制、工艺曲线、节能余热、碳排核算等。
- 覆盖设备：燃气车式窑、车底炉、台车炉、热处理炉、烧成/回火/正火/调质/退火相关系统。
- 开启知识库时，把已入库资料视为优先权威来源；关闭或未命中时再使用通用工程经验。

【角色边界 — 必须严格遵守】
1. 只回答与工业窑炉及上述相关场景的问题。天气、娱乐、其他行业等无关话题须明确拒答，并引导回到窑炉领域。
2. 简短寒暄（如「你好」「在吗」）用 2～4 句话介绍即可：说明可答窑炉问题，开启知识库时可依据已上传手册/规程；禁止大段罗列能力清单或复述本提示全文。
3. 涉及强制性标准、安全规程、碳核算方法学时，尽量给出标准号、章节或方法学编号；知识库有原文则优先用原文。

【知识库使用规则 — 必须严格遵守】
1. 当消息中附有「知识库参考片段」时：优先依据相关片段作答；关键事实、数据、步骤、禁令须能在片段中找到依据，并标注来源（如「依据参考片段 #1」或片段中的手册/章节名）。
2. 多条片段中只有部分相关时：只采用相关内容，不要把无关片段硬凑进答案。
3. 片段未覆盖、明显不相关，或提示「未检索到匹配片段」时：先说明知识库未命中，再给通用经验建议，并标注「⚠️ 该结论非来自知识库」；不得假装引用了知识库。
4. 当提示「本次未启用知识库」时：禁止声称引用了企业知识库，仅基于通用经验与对话上下文作答。
5. 安全禁令、应急处置、联锁与强制性条款：严禁编造；知识库有则严格按片段；无则明确「知识库未检索到对应条款」后再谨慎给出通用注意事项。

【回答风格】
- 结构化中文：核心结论 → 可执行步骤/参数/禁令要点 → 依据（知识库编号或经验标注）。
- 表述专业、简洁，避免空话；不确定时说明不确定，不要臆造数值或条文。"""


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def to_item(row: Prompt, *, include_content: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": row.public_id,
        "name": row.name,
        "remark": row.remark,
        "enabled": bool(row.enabled),
        "createdAt": int(row.created_at.timestamp() * 1000) if row.created_at else 0,
        "updatedAt": int(row.updated_at.timestamp() * 1000) if row.updated_at else 0,
    }
    if include_content:
        item["content"] = row.content
    return item


async def ensure_seed_prompt(db: AsyncSession) -> None:
    """空表时写入一条历史默认提示词，便于管理与对话选择。"""
    result = await db.execute(select(Prompt.id).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    db.add(
        Prompt(
            public_id=short_id(12),
            name=_DEFAULT_SEED_NAME,
            content=_DEFAULT_SEED_CONTENT,
            remark="系统种子：原默认对话提示词",
            enabled=True,
        )
    )
    await db.commit()


async def list_configs(db: AsyncSession) -> list[dict[str, Any]]:
    await ensure_seed_prompt(db)
    result = await db.execute(select(Prompt).order_by(Prompt.updated_at.desc()))
    return [to_item(r) for r in result.scalars().all()]


async def list_options(db: AsyncSession) -> list[dict[str, Any]]:
    """对话页可选：仅启用项，不含全文（减少载荷）。"""
    await ensure_seed_prompt(db)
    result = await db.execute(
        select(Prompt)
        .where(Prompt.enabled.is_(True))
        .order_by(Prompt.updated_at.desc())
    )
    return [to_item(r, include_content=False) for r in result.scalars().all()]


async def get_by_public_id(db: AsyncSession, public_id: str) -> Prompt:
    result = await db.execute(select(Prompt).where(Prompt.public_id == public_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "prompt not found", status_code=404)
    return row


async def get_enabled_content(db: AsyncSession, public_id: str) -> str:
    row = await get_by_public_id(db, public_id)
    if not row.enabled:
        raise AppError(ErrorCode.VALIDATION, "prompt is disabled", status_code=422)
    content = (row.content or "").strip()
    if not content:
        raise AppError(ErrorCode.VALIDATION, "prompt content is empty", status_code=422)
    return content


async def create_config(
    db: AsyncSession,
    *,
    name: str,
    content: str,
    remark: str | None = None,
    enabled: bool = True,
    created_by: int | None = None,
) -> dict[str, Any]:
    if not name.strip() or not content.strip():
        raise AppError(ErrorCode.VALIDATION, "name/content required", status_code=422)
    row = Prompt(
        public_id=short_id(12),
        name=name.strip()[:128],
        content=content.strip(),
        remark=(remark.strip()[:512] if remark and remark.strip() else None),
        enabled=bool(enabled),
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
    content: str | None = None,
    remark: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
        row.name = name.strip()[:128]
    if content is not None:
        if not content.strip():
            raise AppError(ErrorCode.VALIDATION, "content required", status_code=422)
        row.content = content.strip()
    if remark is not None:
        row.remark = remark.strip()[:512] if remark.strip() else None
    if enabled is not None:
        row.enabled = bool(enabled)
    await db.commit()
    await db.refresh(row)
    return to_item(row)


async def delete_config(db: AsyncSession, *, public_id: str) -> None:
    row = await get_by_public_id(db, public_id)
    await db.delete(row)
    await db.commit()
