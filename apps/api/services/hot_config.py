"""热配置：MySQL 权威 + Redis Hash 秒级缓存。

Hash Key：Settings.hot_config_hash_key（默认 hot:config）
Field：config_key
Value：JSON { value, version, enabled, description }
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import get_settings
from common.errors import AppError, ErrorCode
from common.redis_client import get_redis
from db.models.hot_config import HotConfig, HotConfigAudit

# 内置默认 Prompt（DB 无记录时回退）
DEFAULT_PROMPT_SYSTEM_BASE = """你是工业燃气车式窑（车底炉）领域助手，可结合企业知识库中的操作手册、规程、应急预案与标准条文作答。

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

_LEGACY_PROMPT_BRAND = "「炉境 LuJing」"
# 用于识别并升级旧版默认 Prompt（含知识库一句带过的版本）
_PROMPT_KB_SECTION_MARKER = "【知识库使用规则"
_LEGACY_PROMPT_KB_LINE = "优先依据用户提供的「知识库参考片段」作答"

KEY_PROMPT_SYSTEM_BASE = "prompt.system_base"
KEY_PROMPT_ENABLED = "prompt.system_enabled"


def _hash_key() -> str:
    return get_settings().hot_config_hash_key


def _pack(row: HotConfig) -> str:
    return json.dumps(
        {
            "value": row.value,
            "version": row.version,
            "enabled": bool(row.enabled),
            "description": row.description,
        },
        ensure_ascii=False,
    )


def _unpack(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def cache_set(row: HotConfig) -> None:
    redis = get_redis()
    await redis.hset(_hash_key(), row.config_key, _pack(row))


async def cache_delete(config_key: str) -> None:
    redis = get_redis()
    await redis.hdel(_hash_key(), config_key)


async def get_value(
    db: AsyncSession,
    config_key: str,
    *,
    default: str | None = None,
) -> str | None:
    """优先 Redis Hash，未命中读 DB 并回填。"""
    redis = get_redis()
    cached = _unpack(await redis.hget(_hash_key(), config_key))
    if cached is not None:
        if not cached.get("enabled", True):
            return default
        return str(cached.get("value") if cached.get("value") is not None else default)

    result = await db.execute(select(HotConfig).where(HotConfig.config_key == config_key))
    row = result.scalar_one_or_none()
    if row is None:
        return default
    await cache_set(row)
    if not row.enabled:
        return default
    return row.value


async def list_configs(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(HotConfig).order_by(HotConfig.config_key.asc()))
    items = []
    for row in result.scalars().all():
        items.append(
            {
                "key": row.config_key,
                "value": row.value,
                "description": row.description,
                "version": row.version,
                "enabled": bool(row.enabled),
                "updatedBy": row.updated_by,
                "updatedAt": int(row.updated_at.timestamp() * 1000) if row.updated_at else 0,
            }
        )
    return items


async def upsert_config(
    db: AsyncSession,
    *,
    config_key: str,
    value: str,
    description: str | None = None,
    enabled: bool = True,
    operator_id: int | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    key = config_key.strip()
    if not key:
        raise AppError(ErrorCode.VALIDATION, "config_key required", status_code=422)

    result = await db.execute(select(HotConfig).where(HotConfig.config_key == key))
    row = result.scalar_one_or_none()
    action = "update"
    old_value: str | None = None
    if row is None:
        action = "create"
        row = HotConfig(
            config_key=key,
            value=value,
            description=description,
            version=1,
            enabled=enabled,
            updated_by=operator_id,
        )
        db.add(row)
    else:
        old_value = row.value
        row.value = value
        if description is not None:
            row.description = description
        row.enabled = enabled
        row.version = int(row.version or 1) + 1
        row.updated_by = operator_id

    db.add(
        HotConfigAudit(
            config_key=key,
            old_value=old_value,
            new_value=value,
            version=row.version,
            action=action,
            operator_id=operator_id,
            remark=remark,
        )
    )
    await db.commit()
    await db.refresh(row)
    await cache_set(row)
    return {
        "key": row.config_key,
        "value": row.value,
        "description": row.description,
        "version": row.version,
        "enabled": bool(row.enabled),
    }


async def delete_config(
    db: AsyncSession,
    *,
    config_key: str,
    operator_id: int | None = None,
) -> None:
    result = await db.execute(select(HotConfig).where(HotConfig.config_key == config_key))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "config not found", status_code=404)
    db.add(
        HotConfigAudit(
            config_key=config_key,
            old_value=row.value,
            new_value=None,
            version=row.version,
            action="delete",
            operator_id=operator_id,
        )
    )
    await db.delete(row)
    await db.commit()
    await cache_delete(config_key)


async def list_audits(db: AsyncSession, *, config_key: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(HotConfigAudit).order_by(HotConfigAudit.id.desc()).limit(limit)
    if config_key:
        stmt = stmt.where(HotConfigAudit.config_key == config_key)
    result = await db.execute(stmt)
    return [
        {
            "key": a.config_key,
            "oldValue": a.old_value,
            "newValue": a.new_value,
            "version": a.version,
            "action": a.action,
            "operatorId": a.operator_id,
            "remark": a.remark,
            "createdAt": int(a.created_at.timestamp() * 1000) if a.created_at else 0,
        }
        for a in result.scalars().all()
    ]


async def ensure_default_prompts(db: AsyncSession) -> None:
    """种子默认 Prompt；同步去掉旧品牌，并升级仍为旧版知识库规则的默认文案。"""
    result = await db.execute(
        select(HotConfig).where(HotConfig.config_key == KEY_PROMPT_SYSTEM_BASE)
    )
    row = result.scalar_one_or_none()
    if row is None:
        await upsert_config(
            db,
            config_key=KEY_PROMPT_SYSTEM_BASE,
            value=DEFAULT_PROMPT_SYSTEM_BASE,
            description="智能问答系统提示词",
            enabled=True,
            remark="seed default",
        )
    elif row.value and _LEGACY_PROMPT_BRAND in row.value:
        cleaned = row.value.replace(_LEGACY_PROMPT_BRAND, "")
        # 若仍是旧版默认结构，直接换成完整新 Prompt
        if _PROMPT_KB_SECTION_MARKER not in cleaned and _LEGACY_PROMPT_KB_LINE in cleaned:
            cleaned = DEFAULT_PROMPT_SYSTEM_BASE
        await upsert_config(
            db,
            config_key=KEY_PROMPT_SYSTEM_BASE,
            value=cleaned,
            description=row.description or "智能问答系统提示词",
            enabled=bool(row.enabled),
            remark="remove legacy brand / upgrade kb prompt",
        )
    elif (
        row.value
        and _PROMPT_KB_SECTION_MARKER not in row.value
        and _LEGACY_PROMPT_KB_LINE in row.value
    ):
        await upsert_config(
            db,
            config_key=KEY_PROMPT_SYSTEM_BASE,
            value=DEFAULT_PROMPT_SYSTEM_BASE,
            description=row.description or "智能问答系统提示词",
            enabled=bool(row.enabled),
            remark="upgrade default prompt for knowledge base",
        )
    result2 = await db.execute(
        select(HotConfig).where(HotConfig.config_key == KEY_PROMPT_ENABLED)
    )
    if result2.scalar_one_or_none() is None:
        await upsert_config(
            db,
            config_key=KEY_PROMPT_ENABLED,
            value="true",
            description="是否启用自定义系统提示词",
            enabled=True,
            remark="seed default",
        )


async def get_system_prompt_base(db: AsyncSession) -> str:
    await ensure_default_prompts(db)
    enabled = (await get_value(db, KEY_PROMPT_ENABLED, default="true") or "true").lower()
    if enabled in ("0", "false", "off", "no"):
        return DEFAULT_PROMPT_SYSTEM_BASE
    return (
        await get_value(db, KEY_PROMPT_SYSTEM_BASE, default=DEFAULT_PROMPT_SYSTEM_BASE)
        or DEFAULT_PROMPT_SYSTEM_BASE
    )
