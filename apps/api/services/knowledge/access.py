"""知识库权限：查看 / 使用 / 维护。

规则：
  - 超级管理员或 admin 角色：全部库、全部动作
  - 创建人：该库维护（含查看、使用）
  - knowledge_base_acl：按用户或角色叠加；维护 ⊃ 使用 ⊃ 查看
  - 其余人：列表不可见，接口 404（不泄露库是否存在）
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.knowledge.bases import PURPOSE_RAG, get_base_by_public_id, to_base_item
from api.services.menus import user_is_admin
from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeBase, KnowledgeBaseAcl
from db.models.role import Role
from db.models.user import User

PERM_VIEW = "view"
PERM_USE = "use"
PERM_MANAGE = "manage"
Perm = Literal["view", "use", "manage"]

_ALL = frozenset({PERM_VIEW, PERM_USE, PERM_MANAGE})
ALL_PERMS = _ALL
_SUBJECT_USER = "user"
_SUBJECT_ROLE = "role"


def _expand(flags: set[str]) -> set[str]:
    if PERM_MANAGE in flags:
        return set(_ALL)
    if PERM_USE in flags:
        return {PERM_VIEW, PERM_USE}
    return set(flags)


def normalize_flags(*, can_view: bool, can_use: bool, can_manage: bool) -> tuple[bool, bool, bool]:
    manage = bool(can_manage)
    use = bool(can_use) or manage
    view = bool(can_view) or use
    return view, use, manage


def _role_ids(user: User) -> set[int]:
    return {int(r.id) for r in (user.roles or []) if getattr(r, "id", None) is not None}


def _apply_row(flags: set[str], row: KnowledgeBaseAcl) -> None:
    if row.can_manage:
        flags.add(PERM_MANAGE)
    if row.can_use:
        flags.add(PERM_USE)
    if row.can_view:
        flags.add(PERM_VIEW)


async def perms_for_bases(
    db: AsyncSession,
    user: User,
    bases: list[KnowledgeBase],
) -> dict[int, set[str]]:
    if not bases:
        return {}
    if user_is_admin(user):
        return {b.id: set(_ALL) for b in bases}

    out: dict[int, set[str]] = {b.id: set() for b in bases}
    uid = int(user.id)
    for b in bases:
        if b.created_by is not None and int(b.created_by) == uid:
            out[b.id] = set(_ALL)

    ids = [b.id for b in bases]
    result = await db.execute(select(KnowledgeBaseAcl).where(KnowledgeBaseAcl.base_id.in_(ids)))
    roles = _role_ids(user)
    for row in result.scalars().all():
        hit = False
        if row.subject_type == _SUBJECT_USER and int(row.subject_id) == uid:
            hit = True
        elif row.subject_type == _SUBJECT_ROLE and int(row.subject_id) in roles:
            hit = True
        if hit:
            _apply_row(out.setdefault(row.base_id, set()), row)
    return {k: _expand(v) for k, v in out.items()}


async def perms_for_base(db: AsyncSession, user: User, base: KnowledgeBase) -> set[str]:
    mmap = await perms_for_bases(db, user, [base])
    return mmap.get(base.id, set())


def attach_perms(item: dict[str, Any], flags: set[str]) -> dict[str, Any]:
    item["canView"] = PERM_VIEW in flags
    item["canUse"] = PERM_USE in flags
    item["canManage"] = PERM_MANAGE in flags
    return item


async def require_base(
    db: AsyncSession,
    user: User,
    public_id: str,
    perm: Perm,
) -> KnowledgeBase:
    base = await get_base_by_public_id(db, public_id)
    flags = await perms_for_base(db, user, base)
    if perm in flags:
        return base
    if PERM_VIEW not in flags:
        raise AppError(ErrorCode.NOT_FOUND, "knowledge base not found", status_code=404)
    labels = {PERM_USE: "使用（检索）", PERM_MANAGE: "维护"}
    raise AppError(
        ErrorCode.FORBIDDEN,
        f"无权{labels.get(perm, perm)}该知识库",
        status_code=403,
    )


async def list_visible_bases(
    db: AsyncSession,
    user: User,
    *,
    purpose: str | None = PURPOSE_RAG,
    access: Perm = PERM_VIEW,
) -> tuple[list[dict[str, Any]], bool]:
    """返回当前用户可见的库卡片，以及是否允许新建库（仅管理员）。"""
    if access not in _ALL:
        raise AppError(ErrorCode.VALIDATION, "access 仅支持 view / use / manage", status_code=422)
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.status != "deleted")
        .options(selectinload(KnowledgeBase.documents))
        .order_by(KnowledgeBase.updated_at.desc())
    )
    if purpose == PURPOSE_RAG:
        stmt = stmt.where(
            or_(
                KnowledgeBase.purpose == PURPOSE_RAG,
                KnowledgeBase.purpose.is_(None),
            )
        )
    elif purpose is not None:
        stmt = stmt.where(KnowledgeBase.purpose == purpose)
    result = await db.execute(stmt)
    bases = list(result.scalars().all())
    mmap = await perms_for_bases(db, user, bases)
    items: list[dict[str, Any]] = []
    for b in bases:
        flags = mmap.get(b.id, set())
        if access not in flags:
            continue
        items.append(attach_perms(to_base_item(b), flags))
    return items, user_is_admin(user)


async def require_usable_ids(
    db: AsyncSession,
    user: User,
    public_ids: list[str],
) -> list[str]:
    """校验对话/检索传入的库 id，全部须有使用权限。"""
    ordered = [str(x).strip() for x in public_ids if str(x).strip()]
    denied: list[str] = []
    allowed: list[str] = []
    for pid in ordered:
        try:
            await require_base(db, user, pid, PERM_USE)
            allowed.append(pid)
        except AppError as exc:
            if exc.status_code in {403, 404}:
                denied.append(pid)
            else:
                raise
    if denied:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"无权使用知识库：{', '.join(denied)}",
            status_code=403,
        )
    return allowed


async def usable_public_ids(db: AsyncSession, user: User) -> list[str]:
    items, _ = await list_visible_bases(db, user, access=PERM_USE)
    return [str(x["id"]) for x in items]


async def list_acl(db: AsyncSession, user: User, public_id: str) -> dict[str, Any]:
    base = await require_base(db, user, public_id, PERM_MANAGE)
    result = await db.execute(
        select(KnowledgeBaseAcl).where(KnowledgeBaseAcl.base_id == base.id)
    )
    grants: list[dict[str, Any]] = []
    for row in result.scalars().all():
        grants.append(
            {
                "id": row.id,
                "subjectType": row.subject_type,
                "subjectId": int(row.subject_id),
                "canView": bool(row.can_view),
                "canUse": bool(row.can_use),
                "canManage": bool(row.can_manage),
            }
        )
    users_result = await db.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.id.asc())
    )
    roles_result = await db.execute(select(Role).order_by(Role.id.asc()))
    users = [
        {
            "id": u.id,
            "username": u.username,
            "displayName": u.display_name,
            "department": u.department,
        }
        for u in users_result.scalars().all()
    ]
    roles = [
        {"id": r.id, "code": r.code, "name": r.name}
        for r in roles_result.scalars().all()
    ]
    user_map = {int(u["id"]): u for u in users}
    role_map = {int(r["id"]): r for r in roles}
    for g in grants:
        sid = int(g["subjectId"])
        if g["subjectType"] == _SUBJECT_USER:
            u = user_map.get(sid)
            g["subjectLabel"] = (
                (u.get("displayName") or u.get("username") or f"用户#{sid}") if u else f"用户#{sid}"
            )
        else:
            r = role_map.get(sid)
            g["subjectLabel"] = (r.get("name") or f"角色#{sid}") if r else f"角色#{sid}"
    return {
        "baseId": base.public_id,
        "createdBy": base.created_by,
        "grants": grants,
        "directory": {"users": users, "roles": roles},
        "note": "管理员与创建人默认可维护，无需写进授权表。",
    }


async def replace_acl(
    db: AsyncSession,
    user: User,
    public_id: str,
    grants: list[dict[str, Any]],
) -> dict[str, Any]:
    base = await require_base(db, user, public_id, PERM_MANAGE)
    incoming: dict[tuple[str, int], tuple[bool, bool, bool]] = {}
    for raw in grants:
        st = str(raw.get("subjectType") or "").strip().lower()
        if st not in {_SUBJECT_USER, _SUBJECT_ROLE}:
            raise AppError(ErrorCode.VALIDATION, "subjectType 须为 user 或 role", status_code=422)
        try:
            sid = int(raw.get("subjectId"))
        except (TypeError, ValueError) as exc:
            raise AppError(ErrorCode.VALIDATION, "subjectId 无效", status_code=422) from exc
        if sid <= 0:
            raise AppError(ErrorCode.VALIDATION, "subjectId 无效", status_code=422)
        view, use, manage = normalize_flags(
            can_view=bool(raw.get("canView")),
            can_use=bool(raw.get("canUse")),
            can_manage=bool(raw.get("canManage")),
        )
        if not view:
            continue
        incoming[(st, sid)] = (view, use, manage)

    user_ids = [sid for (st, sid) in incoming if st == _SUBJECT_USER]
    role_ids = [sid for (st, sid) in incoming if st == _SUBJECT_ROLE]
    if user_ids:
        found = set(
            (await db.execute(select(User.id).where(User.id.in_(user_ids)))).scalars().all()
        )
        missing = [i for i in user_ids if i not in found]
        if missing:
            raise AppError(ErrorCode.BAD_REQUEST, f"用户不存在：{missing}", status_code=400)
    if role_ids:
        found = set(
            (await db.execute(select(Role.id).where(Role.id.in_(role_ids)))).scalars().all()
        )
        missing = [i for i in role_ids if i not in found]
        if missing:
            raise AppError(ErrorCode.BAD_REQUEST, f"角色不存在：{missing}", status_code=400)

    rows = (
        await db.execute(select(KnowledgeBaseAcl).where(KnowledgeBaseAcl.base_id == base.id))
    ).scalars().all()
    existing_map = {
        (row.subject_type, int(row.subject_id)): row for row in rows
    }
    keep = set(incoming.keys())
    for key, row in existing_map.items():
        if key not in keep:
            await db.delete(row)
    for key, flags in incoming.items():
        st, sid = key
        view, use, manage = flags
        row = existing_map.get(key)
        if row is None:
            db.add(
                KnowledgeBaseAcl(
                    base_id=base.id,
                    subject_type=st,
                    subject_id=sid,
                    can_view=view,
                    can_use=use,
                    can_manage=manage,
                )
            )
        else:
            row.can_view = view
            row.can_use = use
            row.can_manage = manage
    await db.commit()
    return await list_acl(db, user, public_id)
