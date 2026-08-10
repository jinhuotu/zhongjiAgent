"""工作流 CRUD：定义、草稿图、发布、版本查询。"""

from __future__ import annotations

import copy
import secrets
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from common.errors import AppError, ErrorCode
from db.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)

_NODE_TYPES = frozenset({"start", "end", "knowledge", "llm", "agent", "mcp"})


def short_id(n: int = 12) -> str:
    return secrets.token_hex(n // 2 + n % 2)[:n]


def default_empty_graph() -> dict[str, Any]:
    """新建工作流默认图：start → end。"""
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "position": {"x": 80, "y": 160},
                "data": {},
            },
            {
                "id": "end",
                "type": "end",
                "position": {"x": 420, "y": 160},
                "data": {},
            },
        ],
        "edges": [
            {
                "id": "e_start_end",
                "source": "start",
                "target": "end",
            }
        ],
    }


def _ms(dt: Any) -> int:
    if dt is None:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def version_to_item(row: WorkflowVersion) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "version": row.version,
        "status": row.status,
        "graph": row.graph_json if isinstance(row.graph_json, dict) else default_empty_graph(),
        "changelog": row.changelog,
        "createdAt": _ms(row.created_at),
    }


def workflow_to_item(
    row: Workflow,
    *,
    draft: WorkflowVersion | None = None,
    published: WorkflowVersion | None = None,
) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "name": row.name,
        "remark": row.remark,
        "domain": row.domain or "ai",
        "enabled": bool(row.enabled),
        "draftVersion": version_to_item(draft) if draft else None,
        "publishedVersion": version_to_item(published) if published else None,
        "createdAt": _ms(row.created_at),
        "updatedAt": _ms(row.updated_at),
    }


def run_to_item(row: WorkflowRun, *, with_steps: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": row.public_id,
        "workflowId": None,
        "versionId": None,
        "status": row.status,
        "trigger": row.trigger,
        "input": row.input_json if isinstance(row.input_json, dict) else row.input_json,
        "output": row.output_json if isinstance(row.output_json, dict) else row.output_json,
        "errorMsg": row.error_msg,
        "startedAt": _ms(row.started_at),
        "finishedAt": _ms(row.finished_at),
        "createdAt": _ms(row.created_at),
    }
    if with_steps:
        steps = sorted(row.steps or [], key=lambda s: s.id)
        item["steps"] = [
            {
                "id": s.id,
                "nodeId": s.node_id,
                "nodeType": s.node_type,
                "status": s.status,
                "detail": s.detail_json,
                "startedAt": _ms(s.started_at),
                "finishedAt": _ms(s.finished_at),
            }
            for s in steps
        ]
    return item


def validate_graph(graph: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise AppError(ErrorCode.VALIDATION, "graph must be an object", status_code=422)
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise AppError(
            ErrorCode.VALIDATION, "graph.nodes/edges must be arrays", status_code=422
        )
    node_ids: set[str] = set()
    start_count = 0
    end_count = 0
    for n in nodes:
        if not isinstance(n, dict):
            raise AppError(ErrorCode.VALIDATION, "invalid node", status_code=422)
        nid = str(n.get("id") or "").strip()
        ntype = str(n.get("type") or "").strip()
        if not nid:
            raise AppError(ErrorCode.VALIDATION, "node.id required", status_code=422)
        if ntype not in _NODE_TYPES:
            raise AppError(
                ErrorCode.VALIDATION,
                f"unsupported node type: {ntype}",
                status_code=422,
            )
        if nid in node_ids:
            raise AppError(
                ErrorCode.VALIDATION, f"duplicate node id: {nid}", status_code=422
            )
        node_ids.add(nid)
        if ntype == "start":
            start_count += 1
        elif ntype == "end":
            end_count += 1
    if start_count != 1:
        raise AppError(
            ErrorCode.VALIDATION, "graph must have exactly one start node", status_code=422
        )
    if end_count < 1:
        raise AppError(
            ErrorCode.VALIDATION, "graph must have at least one end node", status_code=422
        )
    normalized_edges: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            raise AppError(ErrorCode.VALIDATION, "invalid edge", status_code=422)
        eid = str(e.get("id") or "").strip() or short_id(10)
        src = str(e.get("source") or "").strip()
        tgt = str(e.get("target") or "").strip()
        if src not in node_ids or tgt not in node_ids:
            raise AppError(
                ErrorCode.VALIDATION,
                f"edge references unknown node: {src}->{tgt}",
                status_code=422,
            )
        item: dict[str, Any] = {"id": eid, "source": src, "target": tgt}
        if e.get("sourceHandle") is not None:
            item["sourceHandle"] = e.get("sourceHandle")
        if e.get("targetHandle") is not None:
            item["targetHandle"] = e.get("targetHandle")
        normalized_edges.append(item)
    out_nodes: list[dict[str, Any]] = []
    for n in nodes:
        item = {
            "id": str(n["id"]).strip(),
            "type": str(n["type"]).strip(),
            "data": n.get("data") if isinstance(n.get("data"), dict) else {},
        }
        if isinstance(n.get("position"), dict):
            item["position"] = n["position"]
        out_nodes.append(item)
    return {"nodes": out_nodes, "edges": normalized_edges}


async def get_by_public_id(db: AsyncSession, public_id: str) -> Workflow:
    result = await db.execute(
        select(Workflow)
        .where(Workflow.public_id == public_id)
        .options(selectinload(Workflow.versions))
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "workflow not found", status_code=404)
    return row


async def get_draft_version(
    db: AsyncSession, workflow: Workflow
) -> WorkflowVersion | None:
    result = await db.execute(
        select(WorkflowVersion)
        .where(
            WorkflowVersion.workflow_id == workflow.id,
            WorkflowVersion.status == "draft",
        )
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_published_version(
    db: AsyncSession, workflow: Workflow
) -> WorkflowVersion | None:
    result = await db.execute(
        select(WorkflowVersion)
        .where(
            WorkflowVersion.workflow_id == workflow.id,
            WorkflowVersion.status == "published",
        )
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _max_version(db: AsyncSession, workflow_id: int) -> int:
    result = await db.execute(
        select(WorkflowVersion.version)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    val = result.scalar_one_or_none()
    return int(val or 0)


async def list_published_options(db: AsyncSession) -> list[dict[str, Any]]:
    """登录用户可选：已启用且存在 published 版本的工作流（供报告等引用）。"""
    result = await db.execute(
        select(Workflow)
        .where(Workflow.enabled.is_(True))
        .order_by(Workflow.updated_at.desc())
    )
    items: list[dict[str, Any]] = []
    for row in result.scalars().all():
        published = await get_published_version(db, row)
        if published is None:
            continue
        items.append(
            {
                "id": row.public_id,
                "name": row.name,
                "remark": row.remark,
                "domain": row.domain or "ai",
                "publishedVersion": published.version,
            }
        )
    return items


async def list_workflows(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(Workflow).order_by(Workflow.updated_at.desc()))
    rows = list(result.scalars().all())
    items: list[dict[str, Any]] = []
    for row in rows:
        draft = await get_draft_version(db, row)
        published = await get_published_version(db, row)
        items.append(workflow_to_item(row, draft=draft, published=published))
    return items


async def create_workflow(
    db: AsyncSession,
    *,
    name: str,
    remark: str | None = None,
    domain: str = "ai",
    enabled: bool = True,
    created_by: int | None = None,
) -> dict[str, Any]:
    if not (name or "").strip():
        raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
    domain_norm = (domain or "ai").strip()[:32] or "ai"
    row = Workflow(
        public_id=short_id(12),
        name=name.strip()[:128],
        remark=(remark.strip() if remark and remark.strip() else None),
        domain=domain_norm,
        enabled=bool(enabled),
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    draft = WorkflowVersion(
        public_id=short_id(12),
        workflow_id=row.id,
        version=1,
        status="draft",
        graph_json=default_empty_graph(),
        changelog="初始草稿",
        created_by=created_by,
    )
    db.add(draft)
    await db.commit()
    await db.refresh(row)
    await db.refresh(draft)
    return workflow_to_item(row, draft=draft, published=None)


async def update_workflow(
    db: AsyncSession,
    *,
    public_id: str,
    name: str | None = None,
    remark: str | None = None,
    domain: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
        row.name = name.strip()[:128]
    if remark is not None:
        row.remark = remark.strip() if remark.strip() else None
    if domain is not None:
        row.domain = domain.strip()[:32] or "ai"
    if enabled is not None:
        published_before = await get_published_version(db, row)
        if published_before is None:
            raise AppError(
                ErrorCode.VALIDATION,
                "请先发布工作流后再启停；未发布不可对外引用",
                status_code=422,
            )
        row.enabled = bool(enabled)
    await db.commit()
    await db.refresh(row)
    draft = await get_draft_version(db, row)
    published = await get_published_version(db, row)
    return workflow_to_item(row, draft=draft, published=published)


async def get_workflow(db: AsyncSession, public_id: str) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    draft = await get_draft_version(db, row)
    published = await get_published_version(db, row)
    return workflow_to_item(row, draft=draft, published=published)


async def delete_workflow(db: AsyncSession, *, public_id: str) -> None:
    """按依赖顺序删除：steps → runs → versions → workflow。

    runs.version_id 为 ON DELETE RESTRICT，不能直接删 workflow/version。
    """
    row = await get_by_public_id(db, public_id)
    run_ids = list(
        (
            await db.execute(
                select(WorkflowRun.id).where(WorkflowRun.workflow_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    if run_ids:
        await db.execute(
            delete(WorkflowRunStep).where(WorkflowRunStep.run_id.in_(run_ids))
        )
        await db.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))
    await db.execute(
        delete(WorkflowVersion).where(WorkflowVersion.workflow_id == row.id)
    )
    await db.execute(delete(Workflow).where(Workflow.id == row.id))
    await db.commit()


async def save_graph(
    db: AsyncSession,
    *,
    public_id: str,
    graph: dict[str, Any],
    created_by: int | None = None,
) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    normalized = validate_graph(graph)
    draft = await get_draft_version(db, row)
    if draft is None:
        ver = await _max_version(db, row.id) + 1
        draft = WorkflowVersion(
            public_id=short_id(12),
            workflow_id=row.id,
            version=ver,
            status="draft",
            graph_json=normalized,
            changelog="自动创建草稿",
            created_by=created_by,
        )
        db.add(draft)
    else:
        draft.graph_json = normalized
    await db.commit()
    await db.refresh(row)
    await db.refresh(draft)
    published = await get_published_version(db, row)
    return workflow_to_item(row, draft=draft, published=published)


async def publish_workflow(
    db: AsyncSession,
    *,
    public_id: str,
    changelog: str | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    row = await get_by_public_id(db, public_id)
    draft = await get_draft_version(db, row)
    if draft is None:
        raise AppError(ErrorCode.VALIDATION, "no draft to publish", status_code=422)
    graph = validate_graph(
        draft.graph_json if isinstance(draft.graph_json, dict) else default_empty_graph()
    )
    draft.graph_json = graph
    draft.status = "published"
    if changelog is not None:
        draft.changelog = changelog.strip() or draft.changelog
    # 发布后复制一份新草稿，便于继续编辑
    new_draft = WorkflowVersion(
        public_id=short_id(12),
        workflow_id=row.id,
        version=draft.version + 1,
        status="draft",
        graph_json=copy.deepcopy(graph),
        changelog=None,
        created_by=created_by,
    )
    db.add(new_draft)
    await db.commit()
    await db.refresh(row)
    await db.refresh(draft)
    await db.refresh(new_draft)
    return workflow_to_item(row, draft=new_draft, published=draft)


async def resolve_run_version(
    db: AsyncSession,
    workflow: Workflow,
    *,
    use_draft: bool = False,
) -> WorkflowVersion:
    if use_draft:
        ver = await get_draft_version(db, workflow)
        if ver is None:
            raise AppError(ErrorCode.VALIDATION, "draft version not found", status_code=422)
        return ver
    ver = await get_published_version(db, workflow)
    if ver is None:
        raise AppError(
            ErrorCode.VALIDATION,
            "published version not found; publish first or set useDraft",
            status_code=422,
        )
    return ver


async def list_runs(
    db: AsyncSession,
    *,
    workflow_public_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    wf = await get_by_public_id(db, workflow_public_id)
    result = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == wf.id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(limit)
    )
    items: list[dict[str, Any]] = []
    for run in result.scalars().all():
        item = run_to_item(run)
        item["workflowId"] = wf.public_id
        items.append(item)
    return items


async def get_run(db: AsyncSession, run_public_id: str) -> dict[str, Any]:
    result = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.public_id == run_public_id)
        .options(selectinload(WorkflowRun.steps), selectinload(WorkflowRun.workflow))
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "workflow run not found", status_code=404)
    item = run_to_item(row, with_steps=True)
    item["workflowId"] = row.workflow.public_id if row.workflow else None
    ver = await db.get(WorkflowVersion, row.version_id)
    item["versionId"] = ver.public_id if ver else None
    return item
