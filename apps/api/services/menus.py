"""菜单权限：按角色返回可访问的导航 href（与前端 nav 对齐）。"""

from __future__ import annotations

from db.models.user import User

# 仅管理员可见
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

# 全站菜单目录（与 zhongjivueweb/src/config/nav.ts 对齐的核心路径）
ALL_MENUS: tuple[str, ...] = (
    "/",
    "/realtime",
    "/ai-chat",
    "/ai-reports",
    "/casting-yield",
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

# 非管理员角色的菜单白名单（取并集）；未列出的角色默认「全站减去 adminOnly」
ROLE_MENU_ALLOW: dict[str, frozenset[str]] = {
    "operator": frozenset(
        {
            "/",
            "/realtime",
            "/furnaces",
            "/production/tunnel",
            "/production/batching",
            "/production/shuttle",
            "/alerts",
            "/ai-chat",
            "/knowledge",
        }
    ),
    "auditor": frozenset(
        {
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
        }
    ),
}


def user_is_admin(user: User) -> bool:
    if user.is_superuser:
        return True
    return any(getattr(r, "code", None) == "admin" for r in (user.roles or []))


def resolve_menus(user: User) -> list[str]:
    """返回用户可访问的菜单 href 列表。"""
    if user_is_admin(user):
        return list(ALL_MENUS)

    role_codes = {getattr(r, "code", None) for r in (user.roles or [])}
    role_codes.discard(None)

    restricted = [ROLE_MENU_ALLOW[c] for c in role_codes if c in ROLE_MENU_ALLOW]
    if restricted:
        allowed: set[str] = set()
        for s in restricted:
            allowed |= set(s)
        return sorted(allowed, key=lambda h: ALL_MENUS.index(h) if h in ALL_MENUS else 999)

    # 默认业务角色：可见非 adminOnly 菜单
    return [h for h in ALL_MENUS if h not in ADMIN_ONLY_MENUS]


def can_access_menu(user: User, href: str) -> bool:
    menus = set(resolve_menus(user))
    if href in menus:
        return True
    # 详情页：前缀匹配（如 /knowledge/:id）
    for m in menus:
        if m != "/" and href.startswith(m + "/"):
            return True
    return False
