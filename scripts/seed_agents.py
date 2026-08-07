"""种子：场景智能体示例（幂等，按名称跳过已存在项）。

依赖：
  - 迁移已执行至 0017_scenario_agents
  - 若无提示词，会写入默认「窑炉领域助手」
  - 若库中已有知识库，会绑定第一个（可选）

用法：
  poetry run python scripts/seed_agents.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from sqlalchemy import select

from api.services.agents import configs as agent_configs
from api.services.prompts import configs as prompt_configs
from db.models.agent import ScenarioAgent
from db.models.knowledge import KnowledgeBase
from db.models.prompt import Prompt
from db.session import AsyncSessionLocal

# (name, remark, mode, tools_enabled, bind_first_kb)
SEED_AGENTS: list[tuple[str, str, str, bool, bool]] = [
    (
        "窑炉工况助手",
        "种子：工况 / 工艺问答；绑定默认提示词与首个知识库（若有）",
        "deep",
        True,
        True,
    ),
    (
        "运维知识助手",
        "种子：手册/规程问答；默认关闭 MCP 工具，偏纯检索问答",
        "fast",
        False,
        True,
    ),
]


async def _first_prompt_id(db) -> str | None:  # noqa: ANN001
    await prompt_configs.ensure_seed_prompt(db)
    result = await db.execute(
        select(Prompt)
        .where(Prompt.enabled.is_(True))
        .order_by(Prompt.id.asc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row.public_id if row else None


async def _first_kb_id(db) -> str | None:  # noqa: ANN001
    result = await db.execute(
        select(KnowledgeBase.public_id).order_by(KnowledgeBase.id.asc()).limit(1)
    )
    return result.scalar_one_or_none()


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        prompt_id = await _first_prompt_id(db)
        if not prompt_id:
            raise RuntimeError("no enabled prompt available after seed")
        kb_id = await _first_kb_id(db)
        print(f"[info] promptId={prompt_id}")
        print(f"[info] knowledgeBaseId={kb_id or '(none)'}")

        for name, remark, mode, tools_enabled, bind_kb in SEED_AGENTS:
            exists = await db.execute(
                select(ScenarioAgent.id).where(ScenarioAgent.name == name).limit(1)
            )
            if exists.scalar_one_or_none() is not None:
                print(f"[skip] agent already exists: {name}")
                continue
            kb_ids = [kb_id] if (bind_kb and kb_id) else []
            item = await agent_configs.create_config(
                db,
                name=name,
                remark=remark,
                enabled=True,
                prompt_id=prompt_id,
                knowledge_base_ids=kb_ids,
                mode=mode,
                mcp_tool_ids=[],
                tools_enabled=tools_enabled,
            )
            print(
                f"[ok] created agent: {item['name']} id={item['id']} "
                f"mode={item['mode']} tools={item['toolsEnabled']} kb={item['knowledgeBaseIds']}"
            )


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
