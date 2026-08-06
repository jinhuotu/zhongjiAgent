"""Diagnose MCP MSSQL config + TCP reachability (no password printed)."""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "packages"), str(ROOT)]


def _parse_cs(cs: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cs.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _host_port(server: str) -> tuple[str, int]:
    s = (server or "").strip()
    if s.startswith("tcp:"):
        s = s[4:]
    if "," in s:
        host, port_s = s.split(",", 1)
        try:
            return host.strip(), int(port_s.strip())
        except ValueError:
            return host.strip(), 1433
    if "\\" in s:
        # named instance — TCP port unknown; try 1433
        return s.split("\\", 1)[0].strip(), 1433
    return s, 1433


def tcp_check(host: str, port: int, timeout: float = 5.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return f"OK tcp://{host}:{port}"
    except OSError as exc:
        return f"FAIL tcp://{host}:{port} — {exc}"


async def main() -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from db.models.mcp import McpServer
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(McpServer).options(selectinload(McpServer.tools)))
        rows = list(result.scalars().unique().all())

    print(f"mcp_servers={len(rows)}")
    if not rows:
        print("NO MCP SERVER CONFIGURED")
        return 2

    for s in rows:
        env = s.env if isinstance(s.env, dict) else {}
        cs = str(env.get("MSSQL_CONNECTION_STRING") or "").strip()
        print("=" * 60)
        print(f"id={s.public_id} name={s.name} enabled={s.enabled} timeout={s.timeout_seconds}")
        print(f"command={s.command} args={s.args}")
        print(f"envKeys={sorted(env.keys())}")
        print(f"tools={len(s.tools or [])}")
        if not cs:
            print("MSSQL_CONNECTION_STRING: MISSING")
            continue
        parsed = _parse_cs(cs)
        host_raw = parsed.get("server") or parsed.get("data source") or ""
        dbn = parsed.get("database") or parsed.get("initial catalog") or ""
        user = parsed.get("user id") or parsed.get("uid") or ""
        auth = "sql" if user else ("integrated" if "integrated security" in parsed else "unknown")
        encrypt = parsed.get("encrypt")
        trust = parsed.get("trustservercertificate")
        host, port = _host_port(host_raw)
        print(f"database={dbn or '?'} user={user or '(none)'} auth={auth}")
        print(f"encrypt={encrypt} trustServerCertificate={trust}")
        print(f"serverField={host_raw}")
        print("tcp:", tcp_check(host, port))

        # Also try common alternate ports if named instance
        if "\\" in host_raw and port == 1433:
            print("note: named instance often needs explicit port (Server=host,port)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
