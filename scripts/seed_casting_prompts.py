"""种子：铸造同型号良率相关提示词（幂等，按名称跳过已存在）。

用法：
  venv\\Scripts\\python.exe scripts\\seed_casting_prompts.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "packages"), str(ROOT)]

from sqlalchemy import select

from db.models.prompt import Prompt
from db.session import AsyncSessionLocal

PROMPT_DOC = """你是铸造 MES 工艺与质量分析助手。用户消息里已有完整的结构化分析（含表格）。

你只写解读，禁止改写、摘抄或重新生成任何表格，禁止编造数字。
只输出这四个小节（标题必须原样）：
## 1. 结论摘要
## 8. 推荐生产安排
## 9. 主要缺陷与预防
## 10. 风险与待确认项

规则：
1. 结论必须点明本次查询的订单编号与本行物料；并写明良率/工序是该 InventoryGUID 的同型号历史数据，不是仅本订单。
2. 优先引用「组合良率」推荐组合（班别×电炉×月炉次×箱号）；班组二检排名仅作对照。
3. 样本不足、数据不足处明确写出；气温分箱不得写成因果关系。
4. 不要输出物料档案、库存表、工序表、气温分箱表（这些由系统原样附在文档后部）。
"""

PROMPT_CHAT = """你是铸造排产/良率顾问。用户会提供型号（InventoryGUID）或已生成的最优文档检索结果。

规则：
1. 若上下文标明 found=false 或没有物料档案，直接告知「没有该型号的历史订单/物料档案」，不要编造。
2. 有数据时，优先引用产线良率排名与最优产线，给出可执行的安排建议。
3. 不编造 MES 中不存在的工单号或良率数字。
4. 输出中文，简洁分条。
"""

SEEDS: list[tuple[str, str, str]] = [
    (
        "铸造同型号良率最优文档",
        PROMPT_DOC,
        "工作流 LLM：只写结论/建议；表格由 yield-analysis 原样附上",
    ),
    (
        "铸造同型号排产建议",
        PROMPT_CHAT,
        "场景智能体/工作流 B：同型号排产与检索问答",
    ),
]


async def seed() -> None:
    from api.services.prompts import configs as prompt_configs

    async with AsyncSessionLocal() as db:
        for name, content, remark in SEEDS:
            exists = (
                await db.execute(select(Prompt).where(Prompt.name == name).limit(1))
            ).scalar_one_or_none()
            if exists:
                # 同步更新内容，避免历史 seed 漏掉库存等新结构
                exists.content = content
                exists.remark = remark
                await db.commit()
                print(f"[updated] {name}")
                continue
            item = await prompt_configs.create_config(
                db,
                name=name,
                content=content,
                remark=remark,
                enabled=True,
            )
            print(f"[created] {name} id={item.get('id')}")


if __name__ == "__main__":
    asyncio.run(seed())
