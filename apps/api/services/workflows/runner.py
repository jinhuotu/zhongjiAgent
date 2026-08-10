"""工作流试跑：按边拓扑/BFS 执行，产出 SSE 事件字典。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.workflows import crud as crud_svc
from api.services.workflows import nodes as nodes_svc
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from db.models.workflow import WorkflowRun, WorkflowRunStep

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sse(event: str, payload: dict[str, Any]) -> dict[str, str]:
    return {
        "event": event,
        "data": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _find_start(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [n for n in nodes if str(n.get("type") or "") == "start"]
    if len(starts) != 1:
        raise AppError(
            ErrorCode.VALIDATION,
            "graph must have exactly one start node",
            status_code=422,
        )
    return starts[0]


def _build_adjacency(
    edges: list[dict[str, Any]],
) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for e in edges:
        src = str(e.get("source") or "").strip()
        tgt = str(e.get("target") or "").strip()
        if not src or not tgt:
            continue
        adj.setdefault(src, []).append(tgt)
    return adj


def _linear_order(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """从 start 沿边 BFS；多出边时仅取第一条（MVP 线性偏好）。检测环。"""
    by_id = {str(n.get("id")): n for n in nodes}
    start = _find_start(nodes)
    adj = _build_adjacency(edges)
    order: list[dict[str, Any]] = []
    visited: set[str] = set()
    cur = str(start.get("id"))
    while cur:
        if cur in visited:
            raise AppError(
                ErrorCode.VALIDATION,
                f"cycle detected at node {cur}",
                status_code=422,
            )
        visited.add(cur)
        node = by_id.get(cur)
        if node is None:
            raise AppError(
                ErrorCode.VALIDATION, f"missing node {cur}", status_code=422
            )
        order.append(node)
        if str(node.get("type") or "") == "end":
            break
        outs = adj.get(cur) or []
        cur = outs[0] if outs else ""
    if not order or str(order[-1].get("type") or "") != "end":
        raise AppError(
            ErrorCode.VALIDATION,
            "graph path from start does not reach an end node",
            status_code=422,
        )
    return order


async def run_workflow(
    db: AsyncSession,
    *,
    workflow_public_id: str,
    input_data: Any,
    use_draft: bool = False,
    created_by: int | None = None,
    trigger: str = "trial",
) -> AsyncIterator[dict[str, str]]:
    """创建 run/steps，逐节点执行并 yield SSE 事件。"""
    workflow = await crud_svc.get_by_public_id(db, workflow_public_id)
    if not workflow.enabled:
        raise AppError(ErrorCode.VALIDATION, "workflow is disabled", status_code=422)

    version = await crud_svc.resolve_run_version(db, workflow, use_draft=use_draft)
    graph = (
        version.graph_json
        if isinstance(version.graph_json, dict)
        else crud_svc.default_empty_graph()
    )
    graph = crud_svc.validate_graph(graph)
    order = _linear_order(list(graph["nodes"]), list(graph["edges"]))

    if isinstance(input_data, dict):
        input_json: dict[str, Any] | Any = input_data
    else:
        input_json = {"text": input_data}

    run = WorkflowRun(
        public_id=crud_svc.short_id(12),
        workflow_id=workflow.id,
        version_id=version.id,
        status="pending",
        trigger=trigger,
        input_json=input_json if isinstance(input_json, dict) else {"value": input_json},
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    state: dict[str, Any] = {
        "input": input_data,
        "query": "",
        "context": "",
        "output": None,
        "refs": [],
        "vars": {},
        "lastToolResult": None,
    }

    run.status = "running"
    run.started_at = _now()
    await db.commit()

    try:
        for node in order:
            node_id = str(node.get("id"))
            node_type = str(node.get("type") or "")
            step = WorkflowRunStep(
                run_id=run.id,
                node_id=node_id,
                node_type=node_type,
                status="running",
                started_at=_now(),
            )
            db.add(step)
            await db.commit()
            await db.refresh(step)

            yield _sse(
                "step_start",
                {
                    "runId": run.public_id,
                    "nodeId": node_id,
                    "nodeType": node_type,
                    "stepId": step.id,
                },
            )

            try:
                detail = await nodes_svc.execute_node(db, node=node, state=state)
                step.status = "done"
                step.detail_json = detail
                step.finished_at = _now()
                await db.commit()
                yield _sse(
                    "step_end",
                    {
                        "runId": run.public_id,
                        "nodeId": node_id,
                        "nodeType": node_type,
                        "stepId": step.id,
                        "status": "done",
                        "detail": detail,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                step.status = "failed"
                step.detail_json = {"error": str(exc)}
                step.finished_at = _now()
                run.status = "failed"
                run.error_msg = str(exc)[:512]
                run.finished_at = _now()
                run.output_json = {
                    "output": state.get("output"),
                    "query": state.get("query"),
                    "context": state.get("context"),
                    "refs": state.get("refs"),
                    "lastToolResult": state.get("lastToolResult"),
                }
                await db.commit()
                yield _sse(
                    "step_end",
                    {
                        "runId": run.public_id,
                        "nodeId": node_id,
                        "nodeType": node_type,
                        "stepId": step.id,
                        "status": "failed",
                        "detail": {"error": str(exc)},
                    },
                )
                yield _sse(
                    "error",
                    {"runId": run.public_id, "msg": str(exc), "nodeId": node_id},
                )
                return

        run.status = "done"
        run.finished_at = _now()
        run.output_json = {
            "output": state.get("output"),
            "query": state.get("query"),
            "context": state.get("context"),
            "refs": state.get("refs"),
            "lastToolResult": state.get("lastToolResult"),
            "vars": state.get("vars"),
        }
        await db.commit()
        yield _sse(
            "done",
            {
                "ok": True,
                "runId": run.public_id,
                "status": "done",
                "output": state.get("output"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("workflow run failed run=%s", run.public_id)
        run.status = "failed"
        run.error_msg = str(exc)[:512]
        run.finished_at = _now()
        await db.commit()
        yield _sse("error", {"runId": run.public_id, "msg": str(exc)})
