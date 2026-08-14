"""MCP stdio 可移植路径解析。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps" / "api"))

from services.mcp.client import (  # noqa: E402
    _project_root,
    _resolve_stdio_command_args,
)
from services.mcp.runtime import to_openai_tools  # noqa: E402


def test_project_root_finds_utility_script() -> None:
    root = _project_root()
    assert (root / "scripts" / "mcp_utility_server.py").is_file()


def test_portable_python_and_relative_script() -> None:
    cmd, args = _resolve_stdio_command_args(
        "python", ["scripts/mcp_utility_server.py"]
    )
    assert cmd == sys.executable
    assert Path(args[0]).is_file()
    assert Path(args[0]).name == "mcp_utility_server.py"


def test_placeholder_root() -> None:
    cmd, args = _resolve_stdio_command_args(
        "${PYTHON}", ["${ZHONGJI_ROOT}/scripts/mcp_utility_server.py"]
    )
    assert cmd == sys.executable
    assert Path(args[0]).is_file()


def test_npx_args_not_remapped_to_repo() -> None:
    cmd, args = _resolve_stdio_command_args("npx", ["-y", "mssql-mcp-server"])
    assert "-y" in args
    assert "mssql-mcp-server" in args
    assert not any(a.endswith("\\-y") or a.endswith("/-y") for a in args)


def test_npx_scoped_package_not_remapped_to_repo() -> None:
    """@scope/pkg 含斜杠，不得拼成仓库下的假路径。"""
    pkg = "@modelcontextprotocol/server-filesystem"
    allow = "E:/downLoad"
    cmd, args = _resolve_stdio_command_args("npx", ["-y", pkg, allow])
    assert pkg in args
    assert allow in args
    assert not any("zhongjiAgent" in a and "@modelcontextprotocol" in a for a in args)
    assert not any(a.replace("\\", "/").endswith("/@modelcontextprotocol/server-filesystem") for a in args)


def test_broken_absolute_python_and_script_remap() -> None:
    cmd, args = _resolve_stdio_command_args(
        r"E:\other-machine\.venv\Scripts\python.exe",
        [r"E:\other-machine\scripts\mcp_utility_server.py"],
    )
    assert cmd == sys.executable
    assert Path(args[0]).is_file()
    assert "mcp_utility_server.py" in args[0]


def test_to_openai_tools_dedupes_duplicate_names() -> None:
    tools = to_openai_tools(
        [
            {
                "serverId": "61dc11448dba",
                "name": "get_historical_weather",
                "serverName": "utility",
                "toolId": "091d44911089",
            },
            {
                "serverId": "61dc11448dba",
                "name": "get_historical_weather",
                "serverName": "utility",
                "toolId": "e423931025f8",
            },
        ]
    )
    names = [t["function"]["name"] for t in tools]
    assert names == ["mcp_61dc11448dba_get_historical_weather"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
