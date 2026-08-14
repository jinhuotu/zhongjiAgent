"""种子：注册「通用工具（时间/天气）」MCP Server（幂等）。

依赖：
  - 迁移已含 mcp_servers / mcp_tools
  - 本机可执行当前 Python（stdio 拉起 scripts/mcp_utility_server.py）
  - 天气工具需能访问 Open-Meteo（外网）
  - 历史天气默认厂区：可选环境变量 FACTORY_LAT / FACTORY_LON / FACTORY_NAME

可移植配置（写入库，部署不换路径）：
  - Command: python          → 运行时解析为 API 同 venv 的 sys.executable
  - Args: scripts/mcp_utility_server.py  → 相对仓库根（可用 ${ZHONGJI_ROOT}）

用法：
  poetry run python scripts/seed_mcp_utility.py
"""

from __future__ import annotations

import asyncio
import os
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
# 库内只存可移植写法；启动时由 mcp/client._resolve_stdio_command_args 解析
PORTABLE_COMMAND = "python"
PORTABLE_ARGS = ["scripts/mcp_utility_server.py"]
_REMARK = (
    "内置：get_china_time / get_weather / get_historical_weather（Open-Meteo）。"
    "Command=python、Args=scripts/mcp_utility_server.py（相对仓库，部署无需改路径）。"
    "厂区坐标：FACTORY_LAT/LON/NAME。"
)


def _factory_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("FACTORY_LAT", "FACTORY_LON", "FACTORY_NAME"):
        val = (os.environ.get(key) or "").strip()
        if val:
            env[key] = val
    return env


async def seed() -> None:
    script = ROOT / "scripts" / "mcp_utility_server.py"
    if not script.is_file():
        raise FileNotFoundError(f"missing MCP script: {script}")

    command = PORTABLE_COMMAND
    args = list(PORTABLE_ARGS)
    env = _factory_env()
    if env:
        print(f"[info] factory env: {sorted(env.keys())}")
    else:
        print(
            "[info] 未设置 FACTORY_LAT/LON —— "
            "调用 get_historical_weather 时须显式传 latitude/longitude"
        )
    print(f"[info] portable command={command!r} args={args}")
    print(f"[info] runtime python will be: {sys.executable}")
    print(f"[info] runtime script will be: {script}")

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
                env=env,
                timeout_seconds=60.0,
                remark=_REMARK,
                enabled=True,
            )
            public_id = item["id"]
            print(f"[created] id={public_id} command={command}")
        else:
            public_id = existing.public_id
            # 同步为可移植写法 + 厂区 env（换机后务必再跑一次本脚本或手动改前端表单）
            await mcp_servers.update_server(
                db,
                public_id=public_id,
                command=command,
                args=args,
                env=env if env else None,
                enabled=True,
                remark=_REMARK,
                timeout_seconds=60.0,
            )
            print(f"[updated] id={public_id} command={command}")

        print("[info] probing health + refresh tools …")
        # health_check 内部已 list_tools + 写缓存，不要再 refresh 一次
        health = await mcp_servers.health_check(db, public_id=public_id)
        probe = health.get("probe") or {}
        print(f"[health] ok={health.get('ok')} toolCount={probe.get('toolCount')}")
        refreshed = await mcp_servers.get_by_public_id(db, public_id)
        tools = [
            {"name": t.name, "enabled": t.enabled, "description": t.description}
            for t in (refreshed.tools or [])
        ]
        print(f"[tools] cached={len(tools)}")
        for t in tools:
            print(
                f"  - {t.get('name')}: enabled={t.get('enabled')} "
                f"{(t.get('description') or '')[:80]}"
            )

        print()
        print("下一步：")
        print("  1. 前端编辑框建议：Command=`python`，Args=`[\"scripts/mcp_utility_server.py\"]`")
        print("  2. Env 只放 FACTORY_LAT/LON/NAME（天气工具不需要 MSSQL / Authorization）")
        print("  3. 刷新工具后勾选 get_historical_weather，工作流试跑")


if __name__ == "__main__":
    asyncio.run(seed())
