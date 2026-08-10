"""种子：注册「通用工具（时间/天气）」MCP Server（幂等）。

依赖：
  - 迁移已含 mcp_servers / mcp_tools
  - 本机可执行当前 Python（stdio 拉起 scripts/mcp_utility_server.py）
  - 天气工具需能访问 Open-Meteo（外网）

用法：
  poetry run python scripts/seed_mcp_utility.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from sqlalchemy import select

from api.services.mcp import servers as mcp_servers
from db.models.mcp import McpServer
from db.session import AsyncSessionLocal

SEED_NAME = "通用工具（时间/天气）"


async def seed() -> None:
    script = ROOT / "scripts" / "mcp_utility_server.py"
    if not script.is_file():
        raise FileNotFoundError(f"missing MCP script: {script}")

    command = sys.executable
    args = [str(script)]

    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(McpServer).where(McpServer.name == SEED_NAME).limit(1))
        ).scalar_one_or_none()

        if existing is None:
            item = await mcp_servers.create_server(
                db,
                name=SEED_NAME,
                transport="stdio",
                command=command,
                args=args,
                env={},
                timeout_seconds=60.0,
                remark=(
                    "内置示例：get_china_time / get_weather（Open-Meteo）。"
                    "对话、场景智能体、工作流 MCP 节点均可调用。"
                ),
                enabled=True,
            )
            public_id = item["id"]
            print(f"[created] id={public_id} command={command}")
        else:
            public_id = existing.public_id
            # 同步本机 Python 路径（换机/换 venv 后可再跑本脚本）
            await mcp_servers.update_server(
                db,
                public_id=public_id,
                command=command,
                args=args,
                enabled=True,
                remark=(
                    "内置示例：get_china_time / get_weather（Open-Meteo）。"
                    "对话、场景智能体、工作流 MCP 节点均可调用。"
                ),
            )
            print(f"[updated] id={public_id} command={command}")

        print("[info] probing health + refresh tools …")
        health = await mcp_servers.health_check(db, public_id=public_id)
        probe = health.get("probe") or {}
        print(f"[health] ok={health.get('ok')} toolCount={probe.get('toolCount')}")

        refreshed = await mcp_servers.refresh_tools(db, public_id=public_id)
        tools = refreshed.get("tools") or []
        print(f"[tools] cached={len(tools)}")
        for t in tools:
            print(
                f"  - {t.get('name')}: enabled={t.get('enabled')} "
                f"{(t.get('description') or '')[:80]}"
            )

        print()
        print("下一步：")
        print("  1. 前端「AI 智控 → MCP 管理」确认服务已启用、工具已刷新")
        print("  2. AI 对话开启工具，或场景智能体勾选 get_china_time / get_weather")
        print("  3. 工作流添加「MCP 工具」节点并选择对应工具后试跑")


if __name__ == "__main__":
    asyncio.run(seed())
