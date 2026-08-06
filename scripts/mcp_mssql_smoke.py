"""本地探测 mssql-mcp-server 是否能启动并 list_tools。

用法（PowerShell）:
  $env:MSSQL_CONNECTION_STRING = "Server=...;Database=BestMesDB_JYTONGDA;..."
  $env:TRANSPORT = "stdio"
  poetry run python scripts/mcp_mssql_smoke.py

不传连接串时仅检查全局包能否启动（list_tools 可能因连库失败而报错）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time


async def main() -> int:
    # 保证可从仓库根目录导入 api 包
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    apps_api = os.path.join(root, "apps", "api")
    packages = os.path.join(root, "packages")
    for p in (apps_api, packages, root):
        if p not in sys.path:
            sys.path.insert(0, p)

    from api.services.mcp.client import McpClient, _mssql_mcp_entry_js, _resolve_stdio_command_args

    entry = _mssql_mcp_entry_js()
    print(f"[1] global mssql entry: {entry or 'NOT FOUND'}")
    if not entry:
        print("请先: npm i -g mssql-mcp-server")
        return 2

    cmd, args = _resolve_stdio_command_args("npx", ["-y", "mssql-mcp-server"])
    print(f"[2] resolved launch: {cmd} {' '.join(args)}")

    conn = (os.environ.get("MSSQL_CONNECTION_STRING") or "").strip()
    if not conn:
        print("[3] 未设置 MSSQL_CONNECTION_STRING —— 只会测 MCP 进程启动；连库需自行设置环境变量")
    else:
        # 脱敏打印
        masked = conn
        for key in ("Password=", "Pwd="):
            if key.lower() in conn.lower():
                masked = conn[:40] + "...(masked)"
                break
        print(f"[3] MSSQL_CONNECTION_STRING set ({masked})")

    env = {
        "TRANSPORT": os.environ.get("TRANSPORT") or "stdio",
    }
    if conn:
        env["MSSQL_CONNECTION_STRING"] = conn

    client = McpClient(
        transport="stdio",
        command="mssql-mcp-server",
        args=[],
        env=env,
        timeout_seconds=float(os.environ.get("MCP_SMOKE_TIMEOUT") or 45),
    )

    t0 = time.perf_counter()
    try:
        tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001
        dt = time.perf_counter() - t0
        print(f"[FAIL] list_tools after {dt:.1f}s: {exc}")
        return 1

    dt = time.perf_counter() - t0
    print(f"[OK] list_tools {len(tools)} tools in {dt:.1f}s")
    for t in tools[:12]:
        print(f"  - {t.name}: {(t.description or '')[:80]}")
    if len(tools) > 12:
        print(f"  ... +{len(tools) - 12} more")

    # 若有 list_tables，再试一次真实工具
    names = {t.name for t in tools}
    if "list_tables" in names and conn:
        t1 = time.perf_counter()
        try:
            res = await client.call_tool("list_tables", {})
            print(f"[OK] list_tables in {time.perf_counter() - t1:.1f}s err={res.is_error}")
            print((res.content or "")[:800])
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] list_tables: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
