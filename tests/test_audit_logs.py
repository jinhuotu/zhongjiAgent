"""操作日志路径匹配、IP 提取、保留窗口。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from api.services.audit import (  # noqa: E402
    client_ip,
    error_msg_from_body,
    match_write_target,
    retention_cutoff,
    retention_days,
)


def _request(headers: dict[str, str], client: tuple[str, int] | None = ("10.0.0.8", 443)) -> Request:
    encoded = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "raw_path": b"/api/v1/auth/login",
        "query_string": b"",
        "headers": encoded,
        "client": client,
        "server": ("test", 80),
    }
    return Request(scope)


def test_match_write_covers_required_modules() -> None:
    assert match_write_target("POST", "/api/v1/users") == ("users", "create", None)
    assert match_write_target("PATCH", "/api/v1/users/12") == ("users", "update", "12")
    assert match_write_target("DELETE", "/api/v1/users/12") == ("users", "delete", "12")
    assert match_write_target("POST", "/api/v1/users/12/reset-password") == (
        "users",
        "reset_password",
        "12",
    )
    assert match_write_target("POST", "/api/v1/roles") == ("roles", "create", None)
    assert match_write_target("PATCH", "/api/v1/roles/3") == ("roles", "update", "3")
    assert match_write_target("PUT", "/api/v1/hot-configs") == ("hot_configs", "update", None)
    assert match_write_target("DELETE", "/api/v1/hot-configs/prompt.system_base") == (
        "hot_configs",
        "delete",
        "prompt.system_base",
    )
    assert match_write_target("POST", "/api/v1/mcp-servers") == ("mcp", "create", None)
    assert match_write_target("PATCH", "/api/v1/mcp-servers/abc/tools/t1") == (
        "mcp",
        "update_tool",
        "abc/t1",
    )
    assert match_write_target("POST", "/api/v1/mcp-servers/abc/refresh-tools") == (
        "mcp",
        "refresh_tools",
        "abc",
    )
    assert match_write_target("POST", "/api/v1/models") == ("models", "create", None)
    assert match_write_target("DELETE", "/api/v1/models/m1") == ("models", "delete", "m1")


def test_match_write_skips_reads_and_other_modules() -> None:
    assert match_write_target("GET", "/api/v1/users") is None
    assert match_write_target("GET", "/api/v1/models/runtime") is None
    assert match_write_target("POST", "/api/v1/auth/login") is None
    assert match_write_target("POST", "/api/v1/ai/chat") is None
    assert match_write_target("DELETE", "/api/v1/audit/operations") is None


def test_client_ip_prefers_forwarded_for() -> None:
    req = _request({"x-forwarded-for": "203.0.113.10, 10.0.0.1", "x-real-ip": "10.1.1.1"})
    assert client_ip(req) == "203.0.113.10"


def test_client_ip_falls_back_to_client_host() -> None:
    req = _request({}, client=("192.168.2.20", 1234))
    assert client_ip(req) == "192.168.2.20"


def test_error_msg_from_json_body() -> None:
    body = b'{"code":40100,"msg":"invalid username or password","data":null}'
    assert error_msg_from_body(body, 401) == "invalid username or password"
    assert error_msg_from_body(body, 200) is None


def test_retention_cutoff_is_seven_days() -> None:
    assert retention_days() == 7
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    cutoff = retention_cutoff(now)
    assert (now - cutoff).days == 7
