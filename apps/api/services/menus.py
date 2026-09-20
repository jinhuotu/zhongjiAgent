"""菜单权限：按角色白名单返回可访问的导航 href。管理页只给管理员。"""

from __future__ import annotations

from typing import Any, Iterable

from db.models.user import User

ADMIN_ONLY_MENUS = frozenset(
    {
        "/scene-agents",
        "/model-manage",
        "/prompt-manage",
        "/mcp-manage",
        "/workflows",
        "/users",
        "/logs",
    }
)

ALL_MENUS: tuple[str, ...] = (
    "/",
    "/realtime",
    "/ai-chat",
    "/ai-reports",
    "/casting-yield",
    "/casting-peel",
    "/casting-qa-month",
    "/scene-agents",
    "/model-manage",
    "/prompt-manage",
    "/mcp-manage",
    "/workflows",
    "/knowledge",
    "/data-collect",
    "/data-governance",
    "/decision-flow",
    "/model-package",
    "/production/tunnel",
    "/production/batching",
    "/production/shuttle",
    "/furnaces",
    "/devices",
    "/energy",
    "/energy-flow",
    "/optimization",
    "/budget",
    "/carbon",
    "/verification",
    "/product-footprint",
    "/supply-chain",
    "/carbon-asset",
    "/carbon-market",
    "/policy",
    "/aps/orders",
    "/aps/mps",
    "/aps/overview",
    "/aps/furnace-schedule",
    "/aps/loading",
    "/aps/capacity",
    "/aps/material",
    "/aps/optimization",
    "/aps/energy-schedule",
    "/aps/emergency",
    "/aps/execution",
    "/aps/performance",
    "/quality/overview",
    "/quality/realtime",
    "/quality/prediction",
    "/quality/procurement",
    "/quality/models",
    "/quality/correlation",
    "/quality/trace",
    "/quality/alert",
    "/quality/report",
    "/quality/standard",
    "/quality/test-data",
    "/fault/overview",
    "/fault/realtime",
    "/fault/collection",
    "/fault/prediction",
    "/fault/models",
    "/fault/lifecycle",
    "/fault/alerts",
    "/fault/diagnosis",
    "/fault/maintenance",
    "/fault/spares",
    "/fault/knowledge",
    "/fault/reports",
    "/agents/plan",
    "/agents/procurement",
    "/agents/warehouse",
    "/agents/quality-trace",
    "/agents/vision-inspection",
    "/agents/root-cause",
    "/agents/quality-closed",
    "/agents/ops-workflow",
    "/reports",
    "/alerts",
    "/users",
    "/logs",
    "/settings",
)

BUSINESS_MENUS: tuple[str, ...] = tuple(h for h in ALL_MENUS if h not in ADMIN_ONLY_MENUS)

DEFAULT_NEW_ROLE_MENUS: tuple[str, ...] = ("/", "/ai-chat")

SEED_ROLE_MENUS: dict[str, tuple[str, ...]] = {
    "operator": (
        "/",
        "/realtime",
        "/furnaces",
        "/production/tunnel",
        "/production/batching",
        "/production/shuttle",
        "/alerts",
        "/ai-chat",
        "/knowledge",
    ),
    "auditor": (
        "/",
        "/realtime",
        "/reports",
        "/ai-reports",
        "/alerts",
        "/carbon",
        "/verification",
        "/product-footprint",
        "/energy",
        "/furnaces",
        "/knowledge",
        "/ai-chat",
    ),
    "energy_director": BUSINESS_MENUS,
    "carbon_manager": BUSINESS_MENUS,
    "engineer": BUSINESS_MENUS,
}

_MENU_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("/casting-yield", "/casting-peel", "/casting-qa-month"),
    ("/quality/prediction", "/quality/procurement"),
)


def expand_menu_families(menus: list[str]) -> list[str]:
    merged = set(menus)
    for family in _MENU_FAMILIES:
        if any(h in merged for h in family):
            merged.update(family)
    return _sort_menus(merged)


def user_is_admin(user: User) -> bool:
    if user.is_superuser:
        return True
    return any(getattr(r, "code", None) == "admin" for r in (user.roles or []))


def _sort_menus(hrefs: Iterable[str]) -> list[str]:
    uniq = {h for h in hrefs if h}
    return sorted(uniq, key=lambda h: ALL_MENUS.index(h) if h in ALL_MENUS else 999)


def sanitize_role_menus(raw: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            href = str(item or "").strip()
            if not href or href in seen:
                continue
            if href not in ALL_MENUS or href in ADMIN_ONLY_MENUS:
                continue
            seen.add(href)
            out.append(href)
    if "/" not in seen:
        out.insert(0, "/")
    return expand_menu_families(_sort_menus(out))


def menus_of_role(role: Any) -> list[str]:
    code = getattr(role, "code", None)
    if code == "admin":
        return list(ALL_MENUS)
    raw = getattr(role, "menus", None)
    if isinstance(raw, list) and raw:
        return sanitize_role_menus(raw)
    if code in SEED_ROLE_MENUS:
        return list(SEED_ROLE_MENUS[code])
    return ["/"]


def resolve_menus(user: User) -> list[str]:
    """返回用户可访问的菜单 href 列表。"""
    if user_is_admin(user):
        return list(ALL_MENUS)

    allowed: set[str] = set()
    for role in user.roles or []:
        if getattr(role, "code", None) == "admin":
            return list(ALL_MENUS)
        allowed.update(menus_of_role(role))
    allowed -= ADMIN_ONLY_MENUS
    if not allowed:
        return ["/"]
    return expand_menu_families(_sort_menus(allowed))


def can_access_menu(user: User, href: str) -> bool:
    menus = set(resolve_menus(user))
    if href in menus:
        return True
    for m in menus:
        if m != "/" and href.startswith(m + "/"):
            return True
    return False
