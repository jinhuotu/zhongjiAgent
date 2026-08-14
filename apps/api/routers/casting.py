"""铸造同型号良率分析 API。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from api.deps import CurrentUser, DbSession
from api.schemas.casting import (
    InventorySearchBody,
    OrderItemsBody,
    OrderSearchBody,
    YieldAnalysisBody,
)
from api.services.casting import yield_analysis as yield_svc
from common.errors import AppError
from common.logging import get_logger
from common.response import ok

router = APIRouter(prefix="/casting", tags=["casting"])
logger = get_logger(__name__)

# 文档 SSE 的 done 只回传这些字段，避免把 rawContext+整份分析再推一遍撑爆解析
_DOCUMENT_DONE_KEYS = (
    "found",
    "message",
    "inventoryGuid",
    "markdown",
    "fileExport",
    "document",
    "warnings",
    "needSelect",
    "candidates",
    "query",
)


class YieldDocumentBody(BaseModel):
    inventoryGuid: str | None = Field(default=None, max_length=64)
    query: str | None = Field(
        default=None,
        max_length=128,
        description="名称 / 编码 / GUID；与 inventoryGuid 二选一",
    )
    includeWeather: bool = True
    promptId: str | None = None
    knowledgeBaseId: str | None = Field(
        default=None, description="若传入则把 Markdown 写入该知识库"
    )
    exportToFilesystem: bool = Field(
        default=True,
        description="是否通过 filesystem MCP 将 Markdown 写入本地目录（如 E:/download）",
    )
    mode: str = Field(default="deep", description="fast|deep")
    rawContext: str | None = Field(
        default=None,
        description="若已有查询分析结果，传入则可跳过重复 MES 查询，只跑 LLM 写文档",
    )
    inventory: dict[str, Any] | None = Field(
        default=None,
        description="与 rawContext 配套的物料档案，用于导出文件名",
    )
    orderContext: dict[str, Any] | None = Field(
        default=None,
        description="本次查询订单及本行订货数量，补进文档「本次查询订单」小节",
    )
    insights: dict[str, Any] | None = Field(
        default=None,
        description="查询页解读（结论/推荐/缺陷/风险），导出时与查询页保持一致",
    )

    @model_validator(mode="after")
    def _require_guid_or_query(self) -> Self:
        if not (self.inventoryGuid or "").strip() and not (self.query or "").strip():
            raise ValueError("inventoryGuid 或 query 至少填一个")
        return self


def _exc_msg(exc: BaseException) -> str:
    """httpx/anyio 等异常 str() 经常是空串，前端会误显示成「分析失败」。"""
    if isinstance(exc, AppError) and (exc.msg or "").strip():
        return exc.msg.strip()
    text = (str(exc) or "").strip()
    if text:
        return text
    return type(exc).__name__


def _error_payload(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg": _exc_msg(exc),
        "errorType": type(exc).__name__,
    }
    if isinstance(exc, AppError):
        payload["code"] = int(exc.code)
    return payload


def _document_done_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in _DOCUMENT_DONE_KEYS if key in data}


def _sse(event: str, payload: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(
        event=event,
        data=json.dumps(
            payload, ensure_ascii=False, default=str, allow_nan=False
        ),
    )


def _queued_progress(queue: asyncio.Queue):
    async def _inner(payload: dict[str, Any]) -> None:
        await queue.put(("progress", dict(payload)))

    return _inner


async def _iter_sse(
    queue: asyncio.Queue,
    runner,
    *,
    cancel_on_disconnect: bool = True,
) -> Any:
    """SSE 推送。文档生成在客户端超时断开后仍继续跑完写文件。"""
    task = asyncio.create_task(runner())
    try:
        while True:
            kind, payload = await queue.get()
            try:
                yield _sse(kind, payload)
            except (TypeError, ValueError) as exc:
                logger.exception("casting sse serialize failed kind=%s", kind)
                yield _sse("error", _error_payload(exc))
                break
            if kind in ("done", "error"):
                break
    finally:
        if not task.done() and cancel_on_disconnect:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("casting sse runner failed after stream closed")


@router.post("/inventory-search")
async def casting_inventory_search(
    body: InventorySearchBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """按名称 / 编码 / GUID 检索物料候选。"""
    _ = user
    items = await yield_svc.search_inventories(
        db, query=body.query, top=body.top
    )
    return ok({"query": body.query.strip(), "items": items, "count": len(items)})


@router.post("/order-search")
async def casting_order_search(
    body: OrderSearchBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """按订单编号检索销售订单。"""
    _ = user
    items = await yield_svc.search_sale_orders(
        db, query=body.query, top=body.top
    )
    return ok({"query": body.query.strip(), "items": items, "count": len(items)})


@router.post("/order-items")
async def casting_order_items(
    body: OrderItemsBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """按订单编号 / GUID 加载订货清单物料行。"""
    _ = user
    data = await yield_svc.load_sale_order_items(
        db,
        sale_order_guid=body.saleOrderGuid,
        sale_order_code=body.saleOrderCode,
    )
    return ok(data)


@router.post("/yield-analysis")
async def casting_yield_analysis(
    body: YieldAnalysisBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """按名称/编码/GUID 查询物料档案与产线良率排名。"""
    import time

    from common.logging import get_logger

    _ = user
    log = get_logger(__name__)
    t0 = time.perf_counter()
    log.warning(
        "API POST /casting/yield-analysis start guid=%s query=%s weather=%s",
        body.inventoryGuid,
        body.query,
        body.includeWeather,
    )
    data = await yield_svc.analyze_yield(
        db,
        inventory_guid=body.inventoryGuid,
        query=body.query,
        include_weather=True,
        order_context=body.orderContext,
    )
    log.warning(
        "API POST /casting/yield-analysis done guid=%s found=%s needSelect=%s elapsed=%.1fs",
        data.get("inventoryGuid"),
        data.get("found"),
        data.get("needSelect"),
        time.perf_counter() - t0,
    )
    return ok(data)


@router.post("/yield-analysis/stream")
async def casting_yield_analysis_stream(
    body: YieldAnalysisBody,
    db: DbSession,
    user: CurrentUser,
) -> EventSourceResponse:
    """查询分析（SSE 分步：匹配物料 / MES / 气温 / 对照）。"""
    _ = user
    queue: asyncio.Queue = asyncio.Queue()

    async def runner() -> None:
        try:
            data = await yield_svc.analyze_yield(
                db,
                inventory_guid=body.inventoryGuid,
                query=body.query,
                include_weather=True,
                progress=_queued_progress(queue),
                order_context=body.orderContext,
            )
            await queue.put(("done", data))
        except AppError as exc:
            logger.warning("casting yield-analysis SSE AppError: %s", exc.msg)
            await queue.put(("error", _error_payload(exc)))
        except Exception as exc:  # noqa: ANN001
            logger.exception("casting yield-analysis SSE failed")
            await queue.put(("error", _error_payload(exc)))

    return EventSourceResponse(_iter_sse(queue, runner))


@router.post("/yield-document")
async def casting_yield_document(
    body: YieldDocumentBody,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """分析 + 生成最优良率 Markdown；默认写入本地 filesystem。"""
    data = await yield_svc.generate_yield_document(
        db,
        inventory_guid=body.inventoryGuid,
        query=body.query,
        include_weather=True,
        prompt_id=body.promptId,
        knowledge_base_id=body.knowledgeBaseId,
        export_to_filesystem=body.exportToFilesystem,
        mode=body.mode,
        uploader=user.display_name or user.username,
        raw_context=body.rawContext,
        inventory_snapshot=body.inventory,
        order_context=body.orderContext,
        insights=body.insights,
    )
    return ok(data)


@router.post("/yield-document/stream")
async def casting_yield_document_stream(
    body: YieldDocumentBody,
    db: DbSession,
    user: CurrentUser,
) -> EventSourceResponse:
    """一键生成文档（SSE 分步）。"""
    queue: asyncio.Queue = asyncio.Queue()

    async def runner() -> None:
        ctx_len = len((body.rawContext or "").strip())
        logger.warning(
            "casting yield-document SSE start guid=%s query=%s ctx_len=%s prompt=%s",
            body.inventoryGuid,
            body.query,
            ctx_len,
            body.promptId,
        )
        try:
            data = await yield_svc.generate_yield_document(
                db,
                inventory_guid=body.inventoryGuid,
                query=body.query,
                include_weather=True,
                prompt_id=body.promptId,
                knowledge_base_id=None,
                export_to_filesystem=True,
                mode=body.mode,
                uploader=user.display_name or user.username,
                progress=_queued_progress(queue),
                raw_context=body.rawContext,
                inventory_snapshot=body.inventory,
                order_context=body.orderContext,
                insights=body.insights,
            )
            await queue.put(("done", _document_done_payload(data)))
            logger.warning(
                "casting yield-document SSE done guid=%s markdown_len=%s export_ok=%s",
                data.get("inventoryGuid"),
                len(str(data.get("markdown") or "")),
                (data.get("fileExport") or {}).get("ok")
                if isinstance(data.get("fileExport"), dict)
                else None,
            )
        except AppError as exc:
            logger.warning(
                "casting yield-document SSE AppError: %s", exc.msg
            )
            await queue.put(("error", _error_payload(exc)))
        except Exception as exc:  # noqa: ANN001
            logger.exception("casting yield-document SSE failed")
            await queue.put(("error", _error_payload(exc)))

    return EventSourceResponse(
        _iter_sse(queue, runner, cancel_on_disconnect=False)
    )
