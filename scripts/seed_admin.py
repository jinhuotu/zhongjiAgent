"""初始化种子管理员与角色。

默认账号：
  username: admin
  password: Admin@123456
  display_name: 中机六院管理员

用法：
  poetry run python scripts/seed_admin.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 保证从仓库根目录运行时可导入 packages/*
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from sqlalchemy import select

from api.services.menus import BUSINESS_MENUS, SEED_ROLE_MENUS
from common.security import hash_password
from db.models.role import Role, UserRole
from db.models.user import User
from db.session import AsyncSessionLocal


def _menus_for_seed_role(code: str) -> list[str] | None:
    if code == "admin":
        return None
    return list(SEED_ROLE_MENUS.get(code, BUSINESS_MENUS))

SEED_USERNAME = "admin"
SEED_PASSWORD = "Admin@123456"
SEED_DISPLAY_NAME = "中机六院管理员"
SEED_ROLE_CODE = "admin"

# 与前端「用户与权限」角色矩阵对齐
SEED_ROLES: list[tuple[str, str, str]] = [
    ("admin", "超级管理员", "全部权限"),
    ("energy_director", "能源总监", "运营 + 决策视图"),
    ("carbon_manager", "碳资产经理", "碳市场 + 报表"),
    ("engineer", "工程师", "设备 + 工艺"),
    ("operator", "操作工", "本人当班车式窑只读"),
    ("auditor", "审计员", "全数据只读"),
]


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        role_by_code: dict[str, Role] = {}
        for code, name, description in SEED_ROLES:
            role_result = await db.execute(select(Role).where(Role.code == code))
            role = role_result.scalar_one_or_none()
            if role is None:
                role = Role(
                    code=code,
                    name=name,
                    description=description,
                    menus=_menus_for_seed_role(code),
                )
                db.add(role)
                await db.flush()
                print(f"[ok] created role: {code}")
            else:
                role.name = name
                role.description = description
                if code != "admin" and not role.menus:
                    role.menus = _menus_for_seed_role(code)
                print(f"[ok] updated role: {code}")
            role_by_code[code] = role

        admin_role = role_by_code[SEED_ROLE_CODE]

        user_result = await db.execute(select(User).where(User.username == SEED_USERNAME))
        user = user_result.scalar_one_or_none()
        if user is None:
            user = User(
                username=SEED_USERNAME,
                hashed_password=hash_password(SEED_PASSWORD),
                display_name=SEED_DISPLAY_NAME,
                department="信息中心",
                is_active=True,
                is_superuser=True,
                remark="seed admin",
            )
            db.add(user)
            await db.flush()
            print(f"[ok] created user: {SEED_USERNAME} ({SEED_DISPLAY_NAME})")
        else:
            user.display_name = SEED_DISPLAY_NAME
            user.department = user.department or "信息中心"
            user.is_active = True
            user.is_superuser = True
            user.hashed_password = hash_password(SEED_PASSWORD)
            print(f"[ok] updated user: {SEED_USERNAME} ({SEED_DISPLAY_NAME})")

        link_result = await db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id == admin_role.id,
            )
        )
        if link_result.scalar_one_or_none() is None:
            db.add(UserRole(user_id=user.id, role_id=admin_role.id))
            print("[ok] linked user <-> admin role")
        else:
            print("[skip] user-role link exists")

        await db.commit()

    print()
    print("=== seed complete ===")
    print(f"username : {SEED_USERNAME}")
    print(f"password : {SEED_PASSWORD}")
    print(f"display  : {SEED_DISPLAY_NAME}")
    print(f"roles    : {', '.join(c for c, _, _ in SEED_ROLES)}")


if __name__ == "__main__":
    asyncio.run(seed())
