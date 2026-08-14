"""铸造同型号良率分析服务（骨架）。

数据经已注册的 MSSQL MCP（execute_query）只读查询 BestMES；
良率在服务端计算；无物料档案时 found=false。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.mcp import servers as mcp_servers
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from db.models.mcp import McpServer

logger = get_logger(__name__)

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]] | None


async def _emit_progress(progress: ProgressCb, **payload: Any) -> None:
    if progress is None:
        return
    await progress(payload)

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _sql_escape(value: str) -> str:
    return (value or "").replace("'", "''")


def _parse_mcp_rows(content: Any) -> list[dict[str, Any]]:
    """尽量把 MCP execute_query 返回解析为行字典列表。"""
    if content is None:
        return []
    if isinstance(content, list):
        if content and isinstance(content[0], dict):
            return [dict(x) for x in content]
        return []
    if isinstance(content, dict):
        for key in ("data", "rows", "recordset", "result", "items"):
            val = content.get(key)
            if isinstance(val, list) and (not val or isinstance(val[0], dict)):
                return [dict(x) for x in val]
        return []

    text = str(content).strip()
    if not text:
        return []
    # 可能包一层 markdown / 前缀
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 截取第一个 { 或 [
        for i, ch in enumerate(text):
            if ch in "{[":
                try:
                    parsed = json.loads(text[i:])
                    break
                except json.JSONDecodeError:
                    parsed = None
                    break
        else:
            parsed = None
    if parsed is not None:
        return _parse_mcp_rows(parsed)
    return []


def sql_inventory_by_guid(inventory_guid: str) -> str:
    g = _sql_escape(inventory_guid)
    return f"""
SELECT TOP 1
  i.InventoryGUID,
  i.InventoryCode,
  i.InventoryName,
  i.InventorySpecification,
  i.InventoryCommonName,
  i.InventoryShape,
  i.InventoryPosition,
  i.InventorySizeLength,
  i.InventorySizeWidth,
  i.InventorySizeHeight,
  i.InventoryWeightGross,
  i.InventoryTypeGUID,
  p.SizeA,
  p.SizeB,
  p.SizeH,
  p.Volume,
  p.CutRiserArea,
  p.MachiningArea,
  p.DrillingDepth,
  p.AnnealingDays AS ProductAnnealingDays,
  p.ProductMateralTypeGUID,
  p.ProductTypeGUID,
  p.RiserInventoryGUID,
  mt.ProductMateralTypeName,
  mt.SCastingTypeKey,
  pt.ProductTypeCode,
  pt.ProductTypeName,
  (
    SELECT TOP 1 v.SCastingTypeName
    FROM make.vw_MPSItem v
    WHERE v.InventoryGUID = i.InventoryGUID
  ) AS CastingTypeName,
  r.InventoryCode AS RiserCode,
  r.InventoryName AS RiserName,
  r.InventorySpecification AS RiserSpec,
  r.InventoryWeightGross AS RiserWeightGross
FROM invn.Inventory i
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = i.InventoryGUID
LEFT JOIN comn.ProductMateralType mt ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
LEFT JOIN comn.ProductType pt ON pt.ProductTypeGUID = p.ProductTypeGUID
LEFT JOIN invn.Inventory r ON r.InventoryGUID = p.RiserInventoryGUID
WHERE i.InventoryGUID = '{g}'
""".strip()


def sql_inventory_search(query: str, *, top: int = 20) -> str:
    """按 GUID 精确 / 名称·编码精确优先 + 模糊检索（避免 DISTINCT，兼容 MCP TOP）。"""
    q_raw = (query or "").strip()
    g = _sql_escape(q_raw)
    n = max(1, min(int(top), 50))
    if _GUID_RE.match(q_raw):
        return sql_inventory_by_guid(q_raw)
    # 不查 InventoryCommonName：部分库无此列会导致整句失败
    return f"""
SELECT TOP {n}
  InventoryGUID,
  InventoryCode,
  InventoryName,
  InventorySpecification
FROM invn.Inventory
WHERE InventoryName = N'{g}'
   OR InventoryCode = N'{g}'
   OR InventoryName LIKE N'%{g}%'
   OR InventoryCode LIKE N'%{g}%'
ORDER BY
  CASE
    WHEN InventoryName = N'{g}' THEN 0
    WHEN InventoryCode = N'{g}' THEN 1
    WHEN InventoryName LIKE N'{g}%' THEN 2
    WHEN InventoryCode LIKE N'{g}%' THEN 3
    ELSE 4
  END,
  InventoryCode
""".strip()


def _inventory_row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "inventoryGuid": str(
            _row_get(row, "InventoryGUID", "inventoryGuid", "GUID") or ""
        ),
        "code": _row_get(row, "InventoryCode", "Code", "code"),
        "name": _row_get(row, "InventoryName", "Name", "name"),
        "spec": _row_get(
            row, "InventorySpecification", "Spec", "spec", "InventoryCommonName"
        ),
        "commonName": _row_get(row, "InventoryCommonName", "commonName"),
    }


def _rank_inventory_candidates(
    items: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """精确名称/编码优先，便于一键分析。"""
    q = (query or "").strip().lower()

    def score(it: dict[str, Any]) -> tuple[int, str]:
        name = str(it.get("name") or "").strip().lower()
        code = str(it.get("code") or "").strip().lower()
        common = str(it.get("commonName") or "").strip().lower()
        if name == q or code == q or common == q:
            return (0, code)
        if name.startswith(q) or code.startswith(q):
            return (1, code)
        return (2, code)

    return sorted(items, key=score)


async def search_inventories(
    db: AsyncSession,
    *,
    query: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """按名称/编码/GUID 检索物料候选。"""
    from api.services.mcp.isolated_stdio import (
        run_execute_queries_isolated,
        snapshot_mcp_server,
    )

    q = (query or "").strip()
    if not q:
        raise AppError(ErrorCode.VALIDATION, "query required", status_code=422)

    server = await find_mssql_server(db)
    snap = snapshot_mcp_server(server)
    steps = [("search", sql_inventory_search(q, top=top))]
    try:
        batch = await asyncio.to_thread(
            run_execute_queries_isolated,
            server_snapshot=snap,
            steps=steps,
            timeout_seconds=60.0,
        )
    except AppError:
        raise
    except Exception as e:
        logger.exception("casting.inventory_search failed query=%s", q)
        raise AppError(
            ErrorCode.INTERNAL,
            f"物料检索失败: {e}",
            status_code=500,
        ) from e

    block = batch.get("search") or {}
    # isolated_stdio 只回 raw/content，需与 yield 主流程一样做结构化解析
    rows = block.get("rows")
    if not isinstance(rows, list):
        rows = _parse_mcp_rows(block.get("content") or block.get("raw"))
    items = [
        _inventory_row_to_item(row)
        for row in rows
        if _row_get(row, "InventoryGUID", "inventoryGuid", "GUID")
    ]
    if not items and (block.get("content") or block.get("raw")):
        logger.warning(
            "casting.inventory_search empty_parse query=%s raw_preview=%s",
            q,
            str(block.get("content") or block.get("raw"))[:400],
        )
    else:
        logger.warning(
            "casting.inventory_search ok query=%s hits=%s",
            q,
            len(items),
        )
    return _rank_inventory_candidates(items, q)


def sql_sale_order_search(query: str, *, top: int = 20) -> str:
    """按订单编号精确优先 + 模糊检索。"""
    q_raw = (query or "").strip()
    g = _sql_escape(q_raw)
    n = max(1, min(int(top), 50))
    return f"""
SELECT TOP {n}
  CAST(o.SaleOrderGUID AS nvarchar(36)) AS SaleOrderGUID,
  o.SaleOrderCode,
  CONVERT(varchar(10), o.SaleOrderDate, 23) AS SaleOrderDate,
  o.SSaleOrderStatusKey,
  o.SKilnTypeKey
FROM sale.SaleOrder o
WHERE (
    o.SaleOrderCode = N'{g}'
    OR o.SaleOrderCode LIKE N'{g}%'
    OR o.SaleOrderCode LIKE N'%{g}%'
  )
ORDER BY
  CASE
    WHEN o.SaleOrderCode = N'{g}' THEN 0
    WHEN o.SaleOrderCode LIKE N'{g}%' THEN 1
    ELSE 2
  END,
  o.SaleOrderDate DESC
""".strip()


def sql_sale_order_items(*, sale_order_guid: str | None = None, sale_order_code: str | None = None) -> str:
    """订货清单明细：订单 → SaleOrderItem → 物料档案。"""
    guid = _sql_escape((sale_order_guid or "").strip())
    code = _sql_escape((sale_order_code or "").strip())
    if guid:
        where = f"o.SaleOrderGUID = '{guid}'"
    elif code:
        where = f"o.SaleOrderCode = N'{code}'"
    else:
        raise AppError(
            ErrorCode.VALIDATION,
            "saleOrderGuid 或 saleOrderCode 至少填一个",
            status_code=422,
        )
    return f"""
SELECT TOP 80
  CAST(o.SaleOrderGUID AS nvarchar(36)) AS SaleOrderGUID,
  o.SaleOrderCode,
  CONVERT(varchar(10), o.SaleOrderDate, 23) AS SaleOrderDate,
  o.SSaleOrderStatusKey,
  o.SKilnTypeKey,
  si.ItemIndex,
  CAST(si.InventoryGUID AS nvarchar(36)) AS InventoryGUID,
  inv.InventoryCode,
  inv.InventoryName,
  inv.InventorySpecification,
  mt.ProductMateralTypeName,
  inv.InventoryPosition,
  CAST(si.BillOrderQty AS float) AS BillOrderQty,
  CAST(si.ScheduOrderQty AS float) AS ScheduOrderQty,
  CAST(si.ThisWorkingQty AS float) AS ThisWorkingQty,
  CAST(si.ThisStockQty AS float) AS ThisStockQty,
  CAST(si.ThisMPSingQty AS float) AS ThisMPSingQty,
  CAST(si.DeliveryQty AS float) AS DeliveryQty,
  CAST(si.BillOrderWeight AS float) AS BillOrderWeight
FROM sale.SaleOrderItem si
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = si.SaleOrderGUID
INNER JOIN invn.Inventory inv ON inv.InventoryGUID = si.InventoryGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = inv.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE {where}
ORDER BY si.ItemIndex
""".strip()


def _sale_order_row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    date_s = str(_row_get(row, "SaleOrderDate", "saleOrderDate") or "")[:10]
    return {
        "saleOrderGuid": str(
            _row_get(row, "SaleOrderGUID", "saleOrderGuid", "GUID") or ""
        ),
        "saleOrderCode": _row_get(row, "SaleOrderCode", "saleOrderCode"),
        "saleOrderDate": date_s or None,
        "statusKey": _row_get(row, "SSaleOrderStatusKey", "statusKey"),
        "kilnTypeKey": _row_get(row, "SKilnTypeKey", "kilnTypeKey"),
    }


def _sale_order_item_row(row: dict[str, Any]) -> dict[str, Any]:
    header = _sale_order_row_to_item(row)
    return {
        **header,
        "itemIndex": _as_int(_row_get(row, "ItemIndex", "itemIndex")),
        "inventoryGuid": str(
            _row_get(row, "InventoryGUID", "inventoryGuid", "GUID") or ""
        ),
        "code": _row_get(row, "InventoryCode", "code"),
        "name": _row_get(row, "InventoryName", "name"),
        "spec": _row_get(row, "InventorySpecification", "spec"),
        "materialName": _row_get(row, "ProductMateralTypeName", "materialName"),
        "position": _row_get(row, "InventoryPosition", "position"),
        "billOrderQty": _as_float(_row_get(row, "BillOrderQty", "billOrderQty")),
        "scheduOrderQty": _as_float(_row_get(row, "ScheduOrderQty", "scheduOrderQty")),
        "workingQty": _as_float(_row_get(row, "ThisWorkingQty", "workingQty")),
        "stockQty": _as_float(_row_get(row, "ThisStockQty", "stockQty")),
        "mpsingQty": _as_float(_row_get(row, "ThisMPSingQty", "mpsingQty")),
        "deliveryQty": _as_float(_row_get(row, "DeliveryQty", "deliveryQty")),
        "billOrderWeight": _as_float(_row_get(row, "BillOrderWeight", "billOrderWeight")),
    }


async def _isolated_query_rows(
    db: AsyncSession,
    *,
    step: str,
    sql: str,
    timeout_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    from api.services.mcp.isolated_stdio import (
        run_execute_queries_isolated,
        snapshot_mcp_server,
    )

    server = await find_mssql_server(db)
    snap = snapshot_mcp_server(server)
    batch = await asyncio.to_thread(
        run_execute_queries_isolated,
        server_snapshot=snap,
        steps=[(step, sql)],
        timeout_seconds=timeout_seconds,
    )
    block = batch.get(step) or {}
    rows = block.get("rows")
    if not isinstance(rows, list):
        rows = _parse_mcp_rows(block.get("content") or block.get("raw"))
    return rows


async def search_sale_orders(
    db: AsyncSession,
    *,
    query: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """按订单编号检索销售订单。"""
    q = (query or "").strip()
    if not q:
        raise AppError(ErrorCode.VALIDATION, "query required", status_code=422)
    try:
        rows = await _isolated_query_rows(
            db, step="order_search", sql=sql_sale_order_search(q, top=top)
        )
    except AppError:
        raise
    except Exception as e:
        logger.exception("casting.order_search failed query=%s", q)
        raise AppError(
            ErrorCode.INTERNAL,
            f"订单检索失败: {e}",
            status_code=500,
        ) from e
    items = [
        it
        for it in (_sale_order_row_to_item(row) for row in rows)
        if it.get("saleOrderGuid") or it.get("saleOrderCode")
    ]
    logger.warning("casting.order_search ok query=%s hits=%s", q, len(items))
    return items


async def load_sale_order_items(
    db: AsyncSession,
    *,
    sale_order_guid: str | None = None,
    sale_order_code: str | None = None,
) -> dict[str, Any]:
    """按订单 GUID / 编号加载订货清单物料行。"""
    guid = (sale_order_guid or "").strip()
    code = (sale_order_code or "").strip()
    if not guid and not code:
        raise AppError(
            ErrorCode.VALIDATION,
            "saleOrderGuid 或 saleOrderCode 至少填一个",
            status_code=422,
        )
    try:
        rows = await _isolated_query_rows(
            db,
            step="order_items",
            sql=sql_sale_order_items(sale_order_guid=guid or None, sale_order_code=code or None),
        )
    except AppError:
        raise
    except Exception as e:
        logger.exception(
            "casting.order_items failed guid=%s code=%s", guid, code
        )
        raise AppError(
            ErrorCode.INTERNAL,
            f"订货清单查询失败: {e}",
            status_code=500,
        ) from e
    items = [
        it
        for it in (_sale_order_item_row(row) for row in rows)
        if it.get("inventoryGuid")
    ]
    if not items:
        return {
            "found": False,
            "message": "没有该订单编号的订货清单",
            "order": None,
            "items": [],
            "query": code or guid,
        }
    header = {
        "saleOrderGuid": items[0].get("saleOrderGuid"),
        "saleOrderCode": items[0].get("saleOrderCode"),
        "saleOrderDate": items[0].get("saleOrderDate"),
        "statusKey": items[0].get("statusKey"),
        "kilnTypeKey": items[0].get("kilnTypeKey"),
    }
    logger.warning(
        "casting.order_items ok code=%s lines=%s",
        header.get("saleOrderCode"),
        len(items),
    )
    return {
        "found": True,
        "message": f"订单 {header.get('saleOrderCode')} 共 {len(items)} 种物料",
        "order": header,
        "items": items,
        "query": code or guid,
    }


async def resolve_inventory_query(
    db: AsyncSession,
    *,
    inventory_guid: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """解析为单一 GUID，或返回多候选 / 未找到。

    status: resolved | candidates | not_found
    """
    guid = (inventory_guid or "").strip()
    if guid:
        return {"status": "resolved", "guid": guid, "query": query}

    q = (query or "").strip()
    if not q:
        raise AppError(
            ErrorCode.VALIDATION,
            "inventoryGuid 或 query 至少填一个",
            status_code=422,
        )

    items = await search_inventories(db, query=q, top=20)
    if not items:
        return {
            "status": "not_found",
            "query": q,
            "message": "没有该型号的历史订单/物料档案",
            "candidates": [],
        }
    if len(items) == 1:
        return {
            "status": "resolved",
            "guid": items[0]["inventoryGuid"],
            "query": q,
            "inventory": items[0],
        }

    # 精确名称/编码唯一命中时自动选用
    q_l = q.lower()
    exact = [
        it
        for it in items
        if str(it.get("name") or "").strip().lower() == q_l
        or str(it.get("code") or "").strip().lower() == q_l
        or str(it.get("commonName") or "").strip().lower() == q_l
    ]
    if len(exact) == 1:
        return {
            "status": "resolved",
            "guid": exact[0]["inventoryGuid"],
            "query": q,
            "inventory": exact[0],
        }

    return {
        "status": "candidates",
        "query": q,
        "message": f"找到 {len(items)} 个匹配物料，请选择后继续分析",
        "candidates": items,
    }


def sql_finished_stock(inventory_guid: str) -> str:
    """同型号成品库存（invn.Stock，仅成品相关仓）。

    UUStockQty≈可用；UBStockQty≈占用/锁定；PRStockQty≈在途/预留。
    过滤：仓库名含「成品」且排除「半成品/废品」；另保留常见成品仓编码 A00。
    （全仓结存请看其它查询，避免材料库/半成品库混入「成品库存」）
    """
    g = _sql_escape(inventory_guid)
    return f"""
SELECT
  ISNULL(w.WarehouseCode, N'UNKNOWN') AS WarehouseCode,
  ISNULL(w.WarehouseName, N'未识别仓库') AS WarehouseName,
  SUM(CAST(ISNULL(s.UUStockQty, 0) AS float)) AS AvailableQty,
  SUM(CAST(ISNULL(s.UBStockQty, 0) AS float)) AS LockedQty,
  SUM(CAST(ISNULL(s.PRStockQty, 0) AS float)) AS InTransitQty,
  SUM(
    CAST(ISNULL(s.UUStockQty, 0) AS float)
    + CAST(ISNULL(s.UBStockQty, 0) AS float)
    + CAST(ISNULL(s.PRStockQty, 0) AS float)
  ) AS TotalQty
FROM invn.Stock s
LEFT JOIN invn.Warehouse w ON w.WarehouseGUID = s.WarehouseGUID
WHERE s.InventoryGUID = '{g}'
  AND (
    w.WarehouseCode = N'A00'
    OR (
      w.WarehouseName LIKE N'%成品%'
      AND w.WarehouseName NOT LIKE N'%半成品%'
      AND w.WarehouseName NOT LIKE N'%废品%'
    )
  )
GROUP BY w.WarehouseCode, w.WarehouseName
ORDER BY AvailableQty DESC
""".strip()


def sql_all_stock(inventory_guid: str) -> str:
    """同型号全仓库存（对照用，含半成品/材料等）。"""
    g = _sql_escape(inventory_guid)
    return f"""
SELECT
  ISNULL(w.WarehouseCode, N'UNKNOWN') AS WarehouseCode,
  ISNULL(w.WarehouseName, N'未识别仓库') AS WarehouseName,
  SUM(CAST(ISNULL(s.UUStockQty, 0) AS float)) AS AvailableQty,
  SUM(CAST(ISNULL(s.UBStockQty, 0) AS float)) AS LockedQty,
  SUM(CAST(ISNULL(s.PRStockQty, 0) AS float)) AS InTransitQty,
  SUM(
    CAST(ISNULL(s.UUStockQty, 0) AS float)
    + CAST(ISNULL(s.UBStockQty, 0) AS float)
    + CAST(ISNULL(s.PRStockQty, 0) AS float)
  ) AS TotalQty
FROM invn.Stock s
LEFT JOIN invn.Warehouse w ON w.WarehouseGUID = s.WarehouseGUID
WHERE s.InventoryGUID = '{g}'
GROUP BY w.WarehouseCode, w.WarehouseName
ORDER BY AvailableQty DESC
""".strip()


def sql_yield_by_line(inventory_guid: str) -> str:
    """二检按班组汇总。

    关联：QualitySecondStage.MakingObjectGUID → InventoryMakingObject → InventoryGUID
    产线维：QualityWorkGroupGUID → make.WorkGroup
    合格口径（已确认）：SIdentificationResultKey 为空或以 1 开头 → 合格；
    以 2 开头 → 报废（不合格）。
    """
    g = _sql_escape(inventory_guid)
    return f"""
SELECT
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  SUM(CAST(q.Qty AS float)) AS InputQty,
  SUM(CASE
        WHEN q.SIdentificationResultKey IS NULL THEN CAST(q.Qty AS float)
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '1'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS PassQty,
  SUM(CASE
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '2'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS ScrapQty,
  CASE WHEN SUM(CAST(q.Qty AS float)) > 0
       THEN SUM(CASE
                  WHEN q.SIdentificationResultKey IS NULL THEN CAST(q.Qty AS float)
                  WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '1'
                    THEN CAST(q.Qty AS float)
                  ELSE 0 END) / SUM(CAST(q.Qty AS float))
       ELSE NULL END AS YieldRate
FROM invn.QualitySecondStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
LEFT JOIN make.WorkGroup wg
  ON wg.WorkGroupGUID = q.QualityWorkGroupGUID
WHERE mo.InventoryGUID = '{g}'
GROUP BY wg.WorkGroupCode, wg.WorkGroupName
ORDER BY YieldRate DESC
""".strip()


def sql_second_stage_count(inventory_guid: str) -> str:
    g = _sql_escape(inventory_guid)
    return f"""
SELECT COUNT(1) AS Cnt
FROM invn.QualitySecondStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}'
""".strip()


# 工序气温：StageName → 中文展示名（按远东生产流程三大类展开）
STAGE_WEATHER_LABELS: dict[str, str] = {
    "sand": "砂型班计划",
    "sand_build": "打型任务",
    "sand_paste": "粘型任务",
    "furnace": "电炉计划",
    "casting_plan": "浇铸计划",
    "inbox": "组型任务",
    "casting": "浇铸任务",
    "outbox": "出箱任务",
    "first_inspect": "一检",
    "cut_riser": "切冒计划",
    "modify": "改型计划",
    "cut_inspect": "切检",
    "machining_daily": "加工日计划",
    "machining_task": "加工任务",
    "second_inspect": "二检",
    "scrap": "实物报废",
    "final_inspect": "终检",
}

STAGE_PROCESS_ORDER: tuple[str, ...] = tuple(STAGE_WEATHER_LABELS.keys())

PROCESS_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "sand_ops",
        "label": "砂型作业",
        "stages": ("sand", "sand_build", "sand_paste"),
    },
    {
        "id": "cast_ops",
        "label": "熔铸作业",
        "stages": (
            "furnace",
            "casting_plan",
            "inbox",
            "casting",
            "outbox",
            "first_inspect",
        ),
    },
    {
        "id": "machine_ops",
        "label": "加工作业",
        "stages": (
            "cut_riser",
            "modify",
            "cut_inspect",
            "machining_daily",
            "machining_task",
            "second_inspect",
            "scrap",
            "final_inspect",
        ),
    },
)


def sql_inspect_dates(inventory_guid: str) -> str:
    """兼容旧调用：仅二检录入日。新逻辑请用 sql_process_stage_dates。"""
    g = _sql_escape(inventory_guid)
    return f"""
SELECT CONVERT(varchar(10), q.InputDatetime, 23) AS InspectDate
FROM invn.QualitySecondStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}'
  AND q.InputDatetime IS NOT NULL
GROUP BY CONVERT(varchar(10), q.InputDatetime, 23)
ORDER BY CONVERT(varchar(10), q.InputDatetime, 23)
""".strip()


def _sql_sand_making_dates(g: str, *, stage: str, time_col: str) -> str:
    """打型 / 粘型完工日。"""
    return f"""
SELECT N'{stage}' AS StageName,
       CONVERT(varchar(10), {time_col}, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkMakingObject w
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = w.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND {time_col} IS NOT NULL
GROUP BY CONVERT(varchar(10), {time_col}, 23)
ORDER BY CONVERT(varchar(10), {time_col}, 23)
""".strip()


def _sql_sand_making_detail(
    g: str,
    *,
    stage: str,
    time_col: str,
    finished_col: str,
    group_col: str,
    area_col: str,
) -> str:
    """打型 / 粘型按日+班组明细。"""
    return f"""
SELECT
  N'{stage}' AS StageName,
  CONVERT(varchar(10), {time_col}, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(NULL AS float) AS AllottedWeight,
  SUM(CASE WHEN {finished_col} = N'1' THEN 1 ELSE 0 END) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  CAST(NULL AS nvarchar(64)) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode,
  MAX(mt.ProductMateralTypeName) AS MaterialName,
  MAX(mt.SCastingTypeKey) AS CastingTypeKey,
  MAX(r.InventorySpecification) AS RiserSpec,
  MAX(i.InventoryPosition) AS PositionName,
  MAX({area_col}) AS AreaName
FROM make.WorkMakingObject w
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = w.MakingObjectGUID
INNER JOIN invn.Inventory i ON i.InventoryGUID = mo.InventoryGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = i.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
LEFT JOIN invn.Inventory r ON r.InventoryGUID = p.RiserInventoryGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = {group_col}
WHERE mo.InventoryGUID = '{g}' AND {time_col} IS NOT NULL
GROUP BY CONVERT(varchar(10), {time_col}, 23), wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), {time_col}, 23) DESC
""".strip()


def _sql_inbox_dates(g: str, *, stage: str, date_col: str) -> str:
    """组型 / 浇铸 / 出箱：同一张 WorkCastingInBox，不同日期列。"""
    return f"""
SELECT N'{stage}' AS StageName,
       CONVERT(varchar(10), {date_col}, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND {date_col} IS NOT NULL
GROUP BY CONVERT(varchar(10), {date_col}, 23)
ORDER BY CONVERT(varchar(10), {date_col}, 23)
""".strip()


def _sql_inbox_detail(
    g: str, *, stage: str, date_col: str, with_temps: bool, hold_days: bool = False
) -> str:
    tmp_f = (
        "AVG(CAST(b.FurnaceTMPR AS float))" if with_temps else "CAST(NULL AS float)"
    )
    tmp_c = (
        "AVG(CAST(b.CastingTMPR AS float))" if with_temps else "CAST(NULL AS float)"
    )
    if hold_days:
        tmp_a = (
            "AVG(CASE WHEN b.CastedDate IS NOT NULL AND b.ActualOutDate IS NOT NULL "
            "THEN CAST(DATEDIFF(day, b.CastedDate, b.ActualOutDate) AS float) END)"
        )
    elif with_temps:
        tmp_a = "AVG(CAST(b.AnnealingDays AS float))"
    else:
        tmp_a = "CAST(NULL AS float)"
    tmp_d = (
        "AVG(CAST(b.CastingDurationSS AS float))" if with_temps else "CAST(NULL AS float)"
    )
    return f"""
SELECT
  N'{stage}' AS StageName,
  CONVERT(varchar(10), {date_col}, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(COUNT(1) AS float) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  {tmp_f} AS FurnaceTmpAvg,
  {tmp_c} AS CastingTmpAvg,
  {tmp_a} AS AnnealingDaysAvg,
  {tmp_d} AS CastingDurationAvg,
  CAST(NULL AS nvarchar(64)) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = b.WorkGroupGUID
WHERE mo.InventoryGUID = '{g}' AND {date_col} IS NOT NULL
GROUP BY CONVERT(varchar(10), {date_col}, 23), wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), {date_col}, 23) DESC
""".strip()


def _sql_inbox_mold_detail(g: str) -> str:
    """组型任务：按日+班组+炉次+电炉汇总，对齐 IMES 组型关键列。

    炉次=电炉计划 FurnaceNo；电炉=浇铸计划 FurnaceName；
    部位/冒重/毛重/冒口规格来自物料档案（同型号各行相同）。
    """
    return f"""
SELECT
  N'inbox' AS StageName,
  CONVERT(varchar(10), b.InBoxDate, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(COUNT(1) AS float) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  CAST(NULL AS float) AS CastingDurationAvg,
  CAST(NULL AS nvarchar(64)) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode,
  furn.MonthFurnaceNo AS MonthFurnaceNo,
  furn.FurnaceName AS FurnaceName,
  MAX(i.InventoryPosition) AS PositionName,
  MAX(CAST(i.InventoryWeightGross AS float)) AS GrossWeight,
  MAX(CAST(r.InventoryWeightGross AS float)) AS RiserWeight,
  MAX(r.InventorySpecification) AS RiserSpec
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
INNER JOIN invn.Inventory i ON i.InventoryGUID = mo.InventoryGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = b.WorkGroupGUID
LEFT JOIN invn.Inventory_Product ip ON ip.InventoryGUID = i.InventoryGUID
LEFT JOIN invn.Inventory r ON r.InventoryGUID = ip.RiserInventoryGUID
OUTER APPLY (
  SELECT TOP 1
    CAST(f.FurnaceNo AS nvarchar(64)) AS MonthFurnaceNo,
    p.FurnaceName AS FurnaceName
  FROM make.WorkCastingFurnaceItem fi
  INNER JOIN make.WorkCastingFurnace f
    ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
  LEFT JOIN make.WorkCastingPlan p
    ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
  WHERE fi.MakingObjectGUID = bi.MakingObjectGUID
  ORDER BY COALESCE(fi.CastingTime, f.BusinessDate) DESC
) furn
WHERE mo.InventoryGUID = '{g}' AND b.InBoxDate IS NOT NULL
GROUP BY CONVERT(varchar(10), b.InBoxDate, 23),
         wg.WorkGroupCode, wg.WorkGroupName,
         furn.MonthFurnaceNo, furn.FurnaceName
ORDER BY CONVERT(varchar(10), b.InBoxDate, 23) DESC
""".strip()


def _sql_casting_task_detail(g: str) -> str:
    """浇铸任务：按在制件列出，对齐 IMES 浇铸任务关键列。

    月炉次=WorkCastingFurnace.FurnaceNo；箱号=退火箱号 WorkAnnealingBoxCode；
    班炉次=FurnaceSerialNo；班别=WorkShift（浇铸班）；电炉=浇铸计划 FurnaceName；
    节点=退火窖位 WorkAnnealingCellCode。
    """
    return f"""
SELECT
  N'casting' AS StageName,
  CONVERT(varchar(10), b.CastedDate, 23) AS ProcessDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS LineCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS LineName,
  CAST(1 AS int) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(1 AS float) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(b.FurnaceTMPR AS float) AS FurnaceTmpAvg,
  CAST(b.CastingTMPR AS float) AS CastingTmpAvg,
  CAST(
    CASE
      WHEN b.CastedDate IS NOT NULL AND b.ActualOutDate IS NOT NULL
      THEN DATEDIFF(day, b.CastedDate, b.ActualOutDate)
    END AS float
  ) AS AnnealingDaysAvg,
  CAST(b.CastingDurationSS AS float) AS CastingDurationAvg,
  furn.MonthFurnaceNo AS SamplePlanCode,
  mo.MakingObjectCode AS SampleObjectCode,
  mt.ProductMateralTypeName AS MaterialName,
  mt.SCastingTypeKey AS CastingTypeKey,
  (
    SELECT TOP 1 v.SCastingTypeName
    FROM make.vw_MPSItem v
    WHERE v.InventoryGUID = i.InventoryGUID
  ) AS CastingTypeName,
  r.InventorySpecification AS RiserSpec,
  i.InventoryPosition AS PositionName,
  i.InventorySpecification AS SpecName,
  furn.FurnaceName AS FurnaceName,
  furn.MonthFurnaceNo AS MonthFurnaceNo,
  ISNULL(ab.WorkAnnealingBoxCode, CAST(b.BoxSerialNo AS nvarchar(32))) AS BoxNo,
  CAST(b.FurnaceSerialNo AS nvarchar(32)) AS ShiftFurnaceNo,
  cell.WorkAnnealingCellCode AS NodeCode
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
INNER JOIN invn.Inventory i ON i.InventoryGUID = mo.InventoryGUID
LEFT JOIN make.WorkShift ws
  ON ws.WorkShiftGUID = COALESCE(b.CastWorkShiftGUID, b.WorkShiftGUID)
LEFT JOIN make.WorkAnnealingBox ab
  ON ab.WorkAnnealingBoxGUID = b.WorkAnnealingBoxGUID
LEFT JOIN make.WorkAnnealingCell cell
  ON cell.WorkAnnealingCellGUID = b.WorkAnnealingCellGUID
LEFT JOIN invn.Inventory_Product ip ON ip.InventoryGUID = i.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = ip.ProductMateralTypeGUID
LEFT JOIN invn.Inventory r ON r.InventoryGUID = ip.RiserInventoryGUID
OUTER APPLY (
  SELECT TOP 1
    CAST(f.FurnaceNo AS nvarchar(64)) AS MonthFurnaceNo,
    p.FurnaceName AS FurnaceName
  FROM make.WorkCastingFurnaceItem fi
  INNER JOIN make.WorkCastingFurnace f
    ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
  LEFT JOIN make.WorkCastingPlan p
    ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
  WHERE fi.MakingObjectGUID = bi.MakingObjectGUID
  ORDER BY COALESCE(fi.CastingTime, f.BusinessDate) DESC
) furn
WHERE mo.InventoryGUID = '{g}' AND b.CastedDate IS NOT NULL
ORDER BY b.CastedDate DESC, furn.MonthFurnaceNo DESC, mo.MakingObjectCode DESC
""".strip()


def _sql_quality_dates(g: str, *, stage: str, table: str) -> str:
    return f"""
SELECT N'{stage}' AS StageName,
       CONVERT(varchar(10), q.InputDatetime, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM {table} q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND q.InputDatetime IS NOT NULL
GROUP BY CONVERT(varchar(10), q.InputDatetime, 23)
ORDER BY CONVERT(varchar(10), q.InputDatetime, 23)
""".strip()


def _sql_quality_detail(
    g: str,
    *,
    stage: str,
    table: str,
    qty_expr: str,
) -> str:
    """一检/切检/二检/报废/终检。qty_expr 如 CAST(q.Qty AS float) 或 CAST(1 AS float)。"""
    return f"""
SELECT
  N'{stage}' AS StageName,
  CONVERT(varchar(10), q.InputDatetime, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(NULL AS float) AS FinishedQty,
  SUM({qty_expr}) AS InputQty,
  SUM(CASE
        WHEN q.SIdentificationResultKey IS NULL THEN {qty_expr}
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '1'
          THEN {qty_expr}
        ELSE 0 END) AS PassQty,
  SUM(CASE
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '2'
          THEN {qty_expr}
        ELSE 0 END) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  CAST(NULL AS nvarchar(64)) AS SamplePlanCode,
  MIN(q.MakingObjectCode) AS SampleObjectCode,
  MAX(CASE
        WHEN NULLIF(LTRIM(RTRIM(ISNULL(q.QualityCauseDesc, N''))), N'') IS NULL
          THEN NULL
        ELSE q.QualityCauseDesc END) AS SampleCauseDesc
FROM {table} q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = q.QualityWorkGroupGUID
WHERE mo.InventoryGUID = '{g}' AND q.InputDatetime IS NOT NULL
GROUP BY CONVERT(varchar(10), q.InputDatetime, 23), wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), q.InputDatetime, 23) DESC
""".strip()


def sql_process_stage_dates(inventory_guid: str, stage: str) -> str:
    """单工序日期（砂型班计划/打型/粘型/浇铸/一检/二检）。

    分工序查询，避免 mssql-mcp-server 注入 TOP n 后截断多工序 UNION 结果。
    不用 SELECT DISTINCT / 列名 CreatedDate（MCP 关键字拦截）。
    """
    g = _sql_escape(inventory_guid)
    key = (stage or "").strip().lower()
    if key == "sand":
        return f"""
SELECT N'sand' AS StageName,
       CONVERT(varchar(10), s.InputDatetime, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkPlanSandItem si
INNER JOIN make.WorkPlanSand s ON s.WorkPlanSandGUID = si.WorkPlanSandGUID
WHERE si.InventoryGUID = '{g}' AND s.InputDatetime IS NOT NULL
GROUP BY CONVERT(varchar(10), s.InputDatetime, 23)
ORDER BY CONVERT(varchar(10), s.InputDatetime, 23)
""".strip()
    if key == "sand_build":
        return _sql_sand_making_dates(
            g, stage="sand_build", time_col="w.SandBuildFinishedTime"
        )
    if key == "sand_paste":
        return _sql_sand_making_dates(
            g, stage="sand_paste", time_col="w.SandPasteFinishedTime"
        )
    if key == "furnace":
        return f"""
SELECT N'furnace' AS StageName,
       CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkCastingFurnaceItem fi
INNER JOIN make.WorkCastingFurnace f
  ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = fi.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(fi.CastingTime, f.BusinessDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23)
ORDER BY CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23)
""".strip()
    if key == "casting_plan":
        return f"""
SELECT N'casting_plan' AS StageName,
       CONVERT(varchar(10), p.ProduceDate, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkCastingFurnaceItem fi
INNER JOIN make.WorkCastingFurnace f
  ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
INNER JOIN make.WorkCastingPlan p
  ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = fi.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND p.ProduceDate IS NOT NULL
GROUP BY CONVERT(varchar(10), p.ProduceDate, 23)
ORDER BY CONVERT(varchar(10), p.ProduceDate, 23)
""".strip()
    if key == "inbox":
        return _sql_inbox_dates(g, stage="inbox", date_col="b.InBoxDate")
    if key == "casting":
        return _sql_inbox_dates(g, stage="casting", date_col="b.CastedDate")
    if key == "outbox":
        return _sql_inbox_dates(g, stage="outbox", date_col="b.ActualOutDate")
    if key == "first_inspect":
        return _sql_quality_dates(g, stage="first_inspect", table="invn.QualityFirstStage")
    if key == "cut_riser":
        return f"""
SELECT N'cut_riser' AS StageName,
       CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkCutRiserPlanItem i
INNER JOIN make.WorkCutRiserPlan p
  ON p.WorkCutRiserPlanGUID = i.WorkCutRiserPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(i.FinishedDateTime, p.ProduceDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23)
ORDER BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23)
""".strip()
    if key == "modify":
        return f"""
SELECT N'modify' AS StageName,
       CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkMachingModifyPlanItem i
INNER JOIN make.WorkMachingModifyPlan p
  ON p.WorkMachingModifyPlanGUID = i.WorkMachingModifyPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.SrcMakingObjectGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(i.FinishedDateTime, p.BusinessDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23)
ORDER BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23)
""".strip()
    if key == "cut_inspect":
        return _sql_quality_dates(
            g, stage="cut_inspect", table="invn.QualityCutRiserStage"
        )
    if key == "machining_daily":
        return f"""
SELECT N'machining_daily' AS StageName,
       CONVERT(varchar(10), p.ProduceDate, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkMachingDailyPlanItem i
INNER JOIN make.WorkMachingDailyPlan p
  ON p.WorkMachingDailyPlanGUID = i.WorkMachingDailyPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND p.ProduceDate IS NOT NULL
GROUP BY CONVERT(varchar(10), p.ProduceDate, 23)
ORDER BY CONVERT(varchar(10), p.ProduceDate, 23)
""".strip()
    if key == "machining_task":
        return f"""
SELECT N'machining_task' AS StageName,
       CONVERT(varchar(10), q.QualityDate, 23) AS ProcessDate,
       COUNT(1) AS Cnt
FROM make.WorkMachingQualityOperation q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND q.QualityDate IS NOT NULL
GROUP BY CONVERT(varchar(10), q.QualityDate, 23)
ORDER BY CONVERT(varchar(10), q.QualityDate, 23)
""".strip()
    if key == "second_inspect":
        return _sql_quality_dates(
            g, stage="second_inspect", table="invn.QualitySecondStage"
        )
    if key == "scrap":
        return _sql_quality_dates(g, stage="scrap", table="invn.QualityObjectScrap")
    if key == "final_inspect":
        return _sql_quality_dates(
            g, stage="final_inspect", table="invn.QualityFinalStage"
        )
    raise ValueError(f"unknown process stage: {stage}")


def sql_process_stage_dates_all(inventory_guid: str) -> list[tuple[str, str]]:
    """返回 (stage, sql) 列表，供分析服务逐工序查询。"""
    return [
        (stage, sql_process_stage_dates(inventory_guid, stage))
        for stage in STAGE_WEATHER_LABELS
    ]


def sql_process_stage_detail(inventory_guid: str, stage: str) -> str:
    """工序按日（+班别/班组）工艺对照明细，供气温并列展示。

    砂型班计划：配给量/完工量 + 班别 + 材质/浇筑类型/冒口
    打型/粘型：在制件完工日 + 班组 + 材质/浇筑类型/冒口
    浇铸：炉温/浇铸温/退火天数 + 班组
    一检/二检：投入/合格/报废 + 班组 + 抽样在制件号
    """
    g = _sql_escape(inventory_guid)
    key = (stage or "").strip().lower()
    if key == "sand":
        return f"""
SELECT
  N'sand' AS StageName,
  CONVERT(varchar(10), s.InputDatetime, 23) AS ProcessDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS LineCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS LineName,
  COUNT(1) AS LotCount,
  SUM(CAST(ISNULL(si.AllottedQty, 0) AS float)) AS AllottedQty,
  SUM(CAST(ISNULL(si.AllottedWeight, 0) AS float)) AS AllottedWeight,
  SUM(CAST(ISNULL(si.FinishedQty, 0) AS float)) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(s.WorkPlanSandCode) AS SamplePlanCode,
  CAST(NULL AS nvarchar(64)) AS SampleObjectCode,
  MAX(mt.ProductMateralTypeName) AS MaterialName,
  MAX(mt.SCastingTypeKey) AS CastingTypeKey,
  MAX(r.InventorySpecification) AS RiserSpec,
  MAX(i.InventoryPosition) AS PositionName
FROM make.WorkPlanSandItem si
INNER JOIN make.WorkPlanSand s ON s.WorkPlanSandGUID = si.WorkPlanSandGUID
INNER JOIN invn.Inventory i ON i.InventoryGUID = si.InventoryGUID
LEFT JOIN make.WorkShift ws ON ws.WorkShiftGUID = si.WorkShiftGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = i.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
LEFT JOIN invn.Inventory r ON r.InventoryGUID = p.RiserInventoryGUID
WHERE si.InventoryGUID = '{g}' AND s.InputDatetime IS NOT NULL
GROUP BY CONVERT(varchar(10), s.InputDatetime, 23),
         ws.WorkShiftCode, ws.WorkShiftName, s.WorkPlanSandCode
ORDER BY CONVERT(varchar(10), s.InputDatetime, 23) DESC
""".strip()
    if key == "sand_build":
        return _sql_sand_making_detail(
            g,
            stage="sand_build",
            time_col="w.SandBuildFinishedTime",
            finished_col="w.SandBuildIsFinished",
            group_col="w.SandBuildWorkGroupGUID",
            area_col="w.SandBuildArea",
        )
    if key == "sand_paste":
        return _sql_sand_making_detail(
            g,
            stage="sand_paste",
            time_col="w.SandPasteFinishedTime",
            finished_col="w.SandPasteIsFinished",
            group_col="w.SandPasteWorkGroupGUID",
            area_col="w.SandPasteArea",
        )
    if key == "furnace":
        return f"""
SELECT
  N'furnace' AS StageName,
  CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23) AS ProcessDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS LineCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  SUM(CASE WHEN fi.IsCasted = '1' THEN 1 ELSE 0 END) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(CAST(f.FurnaceNo AS nvarchar(64))) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode,
  MAX(COALESCE(
    NULLIF(LTRIM(RTRIM(mt.ProductMateralTypeName)), N''),
    NULLIF(LTRIM(RTRIM(mt2.ProductMateralTypeName)), N'')
  )) AS MaterialName
FROM make.WorkCastingFurnaceItem fi
INNER JOIN make.WorkCastingFurnace f
  ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = fi.MakingObjectGUID
LEFT JOIN make.WorkShift ws ON ws.WorkShiftGUID = f.WorkShiftGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = f.ProductMateralTypeGUID
LEFT JOIN invn.Inventory_Product ip ON ip.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt2
  ON mt2.ProductMateralTypeGUID = ip.ProductMateralTypeGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(fi.CastingTime, f.BusinessDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23),
         ws.WorkShiftCode, ws.WorkShiftName
ORDER BY CONVERT(varchar(10), COALESCE(fi.CastingTime, f.BusinessDate), 23) DESC
""".strip()
    if key == "casting_plan":
        # 炉次=本型号挂上的电炉计划 FurnaceNo，一行一炉；不用计划头 FurnaceNoList，也不用 STRING_AGG。
        return f"""
SELECT
  N'casting_plan' AS StageName,
  CONVERT(varchar(10), p.ProduceDate, 23) AS ProcessDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS LineCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(NULL AS float) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(p.WorkCastingPlanCode) AS SamplePlanCode,
  MIN(p.FurnaceName) AS AreaName,
  MIN(p.FurnaceName) AS FurnaceName,
  MIN(CAST(f.FurnaceNo AS nvarchar(64))) AS MonthFurnaceNo,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkCastingFurnaceItem fi
INNER JOIN make.WorkCastingFurnace f
  ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
INNER JOIN make.WorkCastingPlan p
  ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = fi.MakingObjectGUID
LEFT JOIN make.WorkShift ws ON ws.WorkShiftGUID = p.WorkShiftGUID
WHERE mo.InventoryGUID = '{g}' AND p.ProduceDate IS NOT NULL
GROUP BY CONVERT(varchar(10), p.ProduceDate, 23),
         ws.WorkShiftCode, ws.WorkShiftName, p.WorkCastingPlanCode, f.FurnaceNo
ORDER BY CONVERT(varchar(10), p.ProduceDate, 23) DESC
""".strip()
    if key == "inbox":
        return _sql_inbox_mold_detail(g)
    if key == "casting":
        return _sql_casting_task_detail(g)
    if key == "outbox":
        return _sql_inbox_detail(
            g,
            stage="outbox",
            date_col="b.ActualOutDate",
            with_temps=False,
            hold_days=True,
        )
    if key == "first_inspect":
        return _sql_quality_detail(
            g,
            stage="first_inspect",
            table="invn.QualityFirstStage",
            qty_expr="CAST(q.Qty AS float)",
        )
    if key == "cut_riser":
        return f"""
SELECT
  N'cut_riser' AS StageName,
  CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  SUM(CASE WHEN i.IsFinished = '1' THEN 1 ELSE 0 END) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(p.WorkCutRiserPlanCode) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkCutRiserPlanItem i
INNER JOIN make.WorkCutRiserPlan p
  ON p.WorkCutRiserPlanGUID = i.WorkCutRiserPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.MakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = p.WorkGroupGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(i.FinishedDateTime, p.ProduceDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23),
         wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.ProduceDate), 23) DESC
""".strip()
    if key == "modify":
        return f"""
SELECT
  N'modify' AS StageName,
  CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  SUM(CASE WHEN i.IsFinished = '1' THEN 1 ELSE 0 END) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(p.WorkMachingModifyPlanCode) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkMachingModifyPlanItem i
INNER JOIN make.WorkMachingModifyPlan p
  ON p.WorkMachingModifyPlanGUID = i.WorkMachingModifyPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.SrcMakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = p.WorkGroupGUID
WHERE mo.InventoryGUID = '{g}'
  AND COALESCE(i.FinishedDateTime, p.BusinessDate) IS NOT NULL
GROUP BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23),
         wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), COALESCE(i.FinishedDateTime, p.BusinessDate), 23) DESC
""".strip()
    if key == "cut_inspect":
        return _sql_quality_detail(
            g,
            stage="cut_inspect",
            table="invn.QualityCutRiserStage",
            qty_expr="CAST(q.Qty AS float)",
        )
    if key == "machining_daily":
        return f"""
SELECT
  N'machining_daily' AS StageName,
  CONVERT(varchar(10), p.ProduceDate, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  SUM(CASE WHEN i.IsFinished = '1' THEN 1 ELSE 0 END) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  MIN(p.WorkMachingDailyPlanCode) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkMachingDailyPlanItem i
INNER JOIN make.WorkMachingDailyPlan p
  ON p.WorkMachingDailyPlanGUID = i.WorkMachingDailyPlanGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = i.MakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = p.WorkGroupGUID
WHERE mo.InventoryGUID = '{g}' AND p.ProduceDate IS NOT NULL
GROUP BY CONVERT(varchar(10), p.ProduceDate, 23), wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), p.ProduceDate, 23) DESC
""".strip()
    if key == "machining_task":
        return f"""
SELECT
  N'machining_task' AS StageName,
  CONVERT(varchar(10), q.QualityDate, 23) AS ProcessDate,
  ISNULL(wg.WorkGroupCode, N'UNKNOWN') AS LineCode,
  ISNULL(wg.WorkGroupName, N'未识别班组') AS LineName,
  COUNT(1) AS LotCount,
  CAST(NULL AS float) AS AllottedQty,
  CAST(COUNT(1) AS float) AS FinishedQty,
  CAST(NULL AS float) AS InputQty,
  CAST(NULL AS float) AS PassQty,
  CAST(NULL AS float) AS ScrapQty,
  CAST(NULL AS float) AS FurnaceTmpAvg,
  CAST(NULL AS float) AS CastingTmpAvg,
  CAST(NULL AS float) AS AnnealingDaysAvg,
  CAST(NULL AS nvarchar(64)) AS SamplePlanCode,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkMachingQualityOperation q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
LEFT JOIN make.WorkGroup wg ON wg.WorkGroupGUID = q.WorkGroupGUID
WHERE mo.InventoryGUID = '{g}' AND q.QualityDate IS NOT NULL
GROUP BY CONVERT(varchar(10), q.QualityDate, 23), wg.WorkGroupCode, wg.WorkGroupName
ORDER BY CONVERT(varchar(10), q.QualityDate, 23) DESC
""".strip()
    if key == "second_inspect":
        return _sql_quality_detail(
            g,
            stage="second_inspect",
            table="invn.QualitySecondStage",
            qty_expr="CAST(q.Qty AS float)",
        )
    if key == "scrap":
        return _sql_quality_detail(
            g,
            stage="scrap",
            table="invn.QualityObjectScrap",
            qty_expr="CAST(q.Qty AS float)",
        )
    if key == "final_inspect":
        return _sql_quality_detail(
            g,
            stage="final_inspect",
            table="invn.QualityFinalStage",
            qty_expr="CAST(1 AS float)",
        )
    raise ValueError(f"unknown process stage detail: {stage}")


def sql_process_stage_details_all(inventory_guid: str) -> list[tuple[str, str]]:
    """返回 (detail_<stage>, sql)，与日期查询分开命名。"""
    return [
        (f"detail_{stage}", sql_process_stage_detail(inventory_guid, stage))
        for stage in STAGE_WEATHER_LABELS
    ]


_SMELT_ELECTRICAL_ROW_LIMIT = 800


def sql_smelt_electrical(inventory_guid: str) -> str:
    """本型号浇铸计划日期+电炉 关联熔化炉报电压/电流（不是月炉次）。"""
    g = _sql_escape(inventory_guid)
    return f"""
SELECT TOP {_SMELT_ELECTRICAL_ROW_LIMIT}
  CONVERT(varchar(10), s.ProduceDate, 23) AS ProduceDate,
  s.FurnaceName,
  ISNULL(ws.WorkShiftName, N'') AS ShiftName,
  si.FurnaceNo AS ShiftFurnaceNo,
  si.ItemIndex,
  si.RecordTime,
  CAST(si.Voltage AS float) AS Voltage,
  CAST(si.ECurrentA AS float) AS ECurrentA,
  CAST(si.ECurrentB AS float) AS ECurrentB,
  CAST(si.ECurrentC AS float) AS ECurrentC
FROM make.WorkCastingSmelt s
INNER JOIN make.WorkCastingSmeltItem si
  ON si.WorkCastingSmeltGUID = s.WorkCastingSmeltGUID
LEFT JOIN make.WorkShift ws ON ws.WorkShiftGUID = s.WorkShiftGUID
WHERE s.ProduceDate IS NOT NULL
  AND (
    si.Voltage IS NOT NULL
    OR si.ECurrentA IS NOT NULL
    OR si.ECurrentB IS NOT NULL
    OR si.ECurrentC IS NOT NULL
  )
  AND EXISTS (
    SELECT 1
    FROM make.WorkCastingFurnaceItem fi
    INNER JOIN make.WorkCastingFurnace f
      ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
    INNER JOIN make.WorkCastingPlan p
      ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
    INNER JOIN invn.InventoryMakingObject mo
      ON mo.MakingObjectGUID = fi.MakingObjectGUID
    WHERE mo.InventoryGUID = '{g}'
      AND p.ProduceDate = s.ProduceDate
      AND p.FurnaceName = s.FurnaceName
  )
ORDER BY s.ProduceDate DESC, si.ItemIndex, si.RecordTime
""".strip()


def sql_yield_by_cast_date(inventory_guid: str) -> str:
    """浇铸日 × 该在制件最终二检结果，用于「浇铸当天气温 vs 良率」。"""
    g = _sql_escape(inventory_guid)
    return f"""
SELECT
  CONVERT(varchar(10), b.CastedDate, 23) AS ProcessDate,
  COUNT(1) AS LotCount,
  SUM(CAST(q.Qty AS float)) AS InputQty,
  SUM(CASE
        WHEN q.SIdentificationResultKey IS NULL THEN CAST(q.Qty AS float)
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '1'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS PassQty,
  SUM(CASE
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '2'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS ScrapQty,
  AVG(CAST(b.FurnaceTMPR AS float)) AS FurnaceTmpAvg,
  AVG(CAST(b.CastingTMPR AS float)) AS CastingTmpAvg
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
INNER JOIN invn.QualitySecondStage q
  ON q.MakingObjectGUID = bi.MakingObjectGUID
WHERE mo.InventoryGUID = '{g}' AND b.CastedDate IS NOT NULL
GROUP BY CONVERT(varchar(10), b.CastedDate, 23)
ORDER BY CONVERT(varchar(10), b.CastedDate, 23) DESC
""".strip()


def sql_yield_by_combo_date(inventory_guid: str) -> str:
    """在制件链路（本版）：浇铸任务 → 二检。

    按浇铸日 + 班别 + 电炉 + 月炉次 + 箱号汇总，供服务端再聚合成组合良率。
    砂型未纳入组合键。
    """
    g = _sql_escape(inventory_guid)
    return f"""
SELECT
  CONVERT(varchar(10), b.CastedDate, 23) AS ProcessDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS ShiftCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS ShiftName,
  ISNULL(furn.FurnaceName, N'未识别电炉') AS FurnaceName,
  ISNULL(furn.MonthFurnaceNo, N'—') AS MonthFurnaceNo,
  ISNULL(ab.WorkAnnealingBoxCode, CAST(b.BoxSerialNo AS nvarchar(32))) AS BoxNo,
  COUNT(1) AS LotCount,
  SUM(CAST(q.Qty AS float)) AS InputQty,
  SUM(CASE
        WHEN q.SIdentificationResultKey IS NULL THEN CAST(q.Qty AS float)
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '1'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS PassQty,
  SUM(CASE
        WHEN LEFT(CAST(q.SIdentificationResultKey AS varchar(16)), 1) = '2'
          THEN CAST(q.Qty AS float)
        ELSE 0 END) AS ScrapQty,
  MIN(mo.MakingObjectCode) AS SampleObjectCode
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
INNER JOIN invn.QualitySecondStage q
  ON q.MakingObjectGUID = bi.MakingObjectGUID
LEFT JOIN make.WorkShift ws
  ON ws.WorkShiftGUID = COALESCE(b.CastWorkShiftGUID, b.WorkShiftGUID)
LEFT JOIN make.WorkAnnealingBox ab
  ON ab.WorkAnnealingBoxGUID = b.WorkAnnealingBoxGUID
OUTER APPLY (
  SELECT TOP 1
    CAST(f.FurnaceNo AS nvarchar(64)) AS MonthFurnaceNo,
    p.FurnaceName AS FurnaceName
  FROM make.WorkCastingFurnaceItem fi
  INNER JOIN make.WorkCastingFurnace f
    ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
  LEFT JOIN make.WorkCastingPlan p
    ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
  WHERE fi.MakingObjectGUID = bi.MakingObjectGUID
  ORDER BY COALESCE(fi.CastingTime, f.BusinessDate) DESC
) furn
WHERE mo.InventoryGUID = '{g}' AND b.CastedDate IS NOT NULL
GROUP BY CONVERT(varchar(10), b.CastedDate, 23),
         ws.WorkShiftCode, ws.WorkShiftName,
         furn.FurnaceName, furn.MonthFurnaceNo,
         ISNULL(ab.WorkAnnealingBoxCode, CAST(b.BoxSerialNo AS nvarchar(32)))
ORDER BY CONVERT(varchar(10), b.CastedDate, 23) DESC
""".strip()


_PROCESS_DETAIL_ROW_LIMIT = 80


def _parse_process_detail_row(row: dict[str, Any]) -> dict[str, Any] | None:
    date_s = str(_row_get(row, "ProcessDate", "processDate") or "")[:10]
    if len(date_s) < 10:
        return None
    stage = str(_row_get(row, "StageName", "stageName") or "").strip()
    item: dict[str, Any] = {
        "stage": stage,
        "processDate": date_s,
        "lineCode": _row_get(row, "LineCode", "lineCode"),
        "lineName": _row_get(row, "LineName", "lineName"),
        "lotCount": _as_int(_row_get(row, "LotCount", "lotCount")),
        "allottedQty": _as_float(_row_get(row, "AllottedQty", "allottedQty")),
        "allottedWeight": _as_float(_row_get(row, "AllottedWeight", "allottedWeight")),
        "finishedQty": _as_float(_row_get(row, "FinishedQty", "finishedQty")),
        "inputQty": _as_float(_row_get(row, "InputQty", "inputQty")),
        "passQty": _as_float(_row_get(row, "PassQty", "passQty")),
        "scrapQty": _as_float(_row_get(row, "ScrapQty", "scrapQty")),
        "furnaceTmpAvg": _as_float(_row_get(row, "FurnaceTmpAvg", "furnaceTmpAvg")),
        "castingTmpAvg": _as_float(_row_get(row, "CastingTmpAvg", "castingTmpAvg")),
        "annealingDaysAvg": _as_float(
            _row_get(row, "AnnealingDaysAvg", "annealingDaysAvg")
        ),
        "castingDurationAvg": _as_float(
            _row_get(row, "CastingDurationAvg", "castingDurationAvg")
        ),
        "samplePlanCode": _row_get(row, "SamplePlanCode", "samplePlanCode"),
        "sampleObjectCode": _row_get(row, "SampleObjectCode", "sampleObjectCode"),
        "sampleCauseDesc": _row_get(row, "SampleCauseDesc", "sampleCauseDesc"),
        "materialName": _row_get(row, "MaterialName", "materialName"),
        "castingTypeKey": _row_get(row, "CastingTypeKey", "castingTypeKey"),
        "castingTypeName": _row_get(row, "CastingTypeName", "castingTypeName"),
        "riserSpec": _row_get(row, "RiserSpec", "riserSpec"),
        "position": _row_get(row, "PositionName", "position"),
        "areaName": _row_get(row, "AreaName", "areaName"),
        "spec": _row_get(row, "SpecName", "spec"),
        "monthFurnaceNo": _row_get(row, "MonthFurnaceNo", "monthFurnaceNo"),
        "boxNo": _row_get(row, "BoxNo", "boxNo"),
        "shiftFurnaceNo": _row_get(row, "ShiftFurnaceNo", "shiftFurnaceNo"),
        "furnaceName": _row_get(row, "FurnaceName", "furnaceName"),
        "nodeCode": _row_get(row, "NodeCode", "nodeCode"),
        "grossWeight": _as_float(_row_get(row, "GrossWeight", "grossWeight")),
        "riserWeight": _as_float(_row_get(row, "RiserWeight", "riserWeight")),
    }
    inp = item.get("inputQty")
    pas = item.get("passQty")
    if isinstance(inp, (int, float)) and inp > 0 and isinstance(pas, (int, float)):
        item["yieldRate"] = round(float(pas) / float(inp), 4)
    else:
        item["yieldRate"] = None
    return item


def _collect_process_details_from_batch(
    batch: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """从 batch detail_* 步解析工序工艺行。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for stage in STAGE_WEATHER_LABELS:
        block = batch.get(f"detail_{stage}") or {}
        rows_raw = block.get("rows") or []
        parsed: list[dict[str, Any]] = []
        for row in rows_raw:
            item = _parse_process_detail_row(row)
            if item:
                parsed.append(item)
        # SQL 已按日期 DESC；再截断，避免 SSE / 页面过重（电炉按日+班别聚合后通常 <80）
        out[stage] = parsed[:_PROCESS_DETAIL_ROW_LIMIT]
        extra = len(parsed) - len(out[stage])
        if extra > 0:
            for row in out[stage][:1]:
                row["_truncatedHint"] = extra
    return out


def _smelt_at_iso(produce_date: str, record_time: Any) -> str | None:
    day = str(produce_date or "")[:10]
    if len(day) < 10:
        return None
    rec = str(record_time or "").strip()
    time_part = "00:00:00"
    if "T" in rec:
        time_part = (rec.split("T", 1)[1] + "00:00:00")[:8]
        year = rec[:4]
        if year.isdigit() and int(year) >= 2000:
            return rec[:19].replace("Z", "")
    elif " " in rec:
        time_part = (rec.split(" ", 1)[1] + "00:00:00")[:8]
    if time_part[2] != ":":
        time_part = "00:00:00"
    return f"{day}T{time_part}"


def _parse_smelt_electrical_from_batch(batch: dict[str, Any]) -> dict[str, Any]:
    block = batch.get("smelt_electrical") or {}
    points: list[dict[str, Any]] = []
    for row in block.get("rows") or []:
        day = str(_row_get(row, "ProduceDate", "produceDate") or "")[:10]
        at = _smelt_at_iso(day, _row_get(row, "RecordTime", "recordTime"))
        if not at:
            continue
        volt = _as_float(_row_get(row, "Voltage", "voltage"))
        ca = _as_float(_row_get(row, "ECurrentA", "eCurrentA"))
        cb = _as_float(_row_get(row, "ECurrentB", "eCurrentB"))
        cc = _as_float(_row_get(row, "ECurrentC", "eCurrentC"))
        if volt is None and ca is None and cb is None and cc is None:
            continue
        points.append(
            {
                "at": at,
                "produceDate": day,
                "furnaceName": str(_row_get(row, "FurnaceName", "furnaceName") or "").strip()
                or "未识别",
                "shiftName": _row_get(row, "ShiftName", "shiftName"),
                "shiftFurnaceNo": _row_get(row, "ShiftFurnaceNo", "shiftFurnaceNo"),
                "voltage": volt,
                "currentA": ca,
                "currentB": cb,
                "currentC": cc,
            }
        )
    points.sort(key=lambda x: (str(x.get("at") or ""), str(x.get("furnaceName") or "")))
    return {
        "note": (
            "熔化炉报电压/电流：按本型号浇铸计划的日期+电炉关联，"
            "不是单个月炉次；横轴为记录时刻（无日期时用生产日拼接）。"
        ),
        "pointCount": len(points),
        "truncated": len(block.get("rows") or []) >= _SMELT_ELECTRICAL_ROW_LIMIT,
        "points": points,
    }


def _attach_outdoor_temp_to_process_rows(
    process_details: dict[str, list[dict[str, Any]]],
    weather_summary: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """给工艺行挂上当日厂区室外均温。"""
    by_day: dict[str, Any] = {}
    if weather_summary:
        for d in weather_summary.get("days") or []:
            key = str(d.get("date") or "")[:10]
            if key:
                by_day[key] = d
    enriched: dict[str, list[dict[str, Any]]] = {}
    for stage, rows in process_details.items():
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            r = dict(row)
            day = by_day.get(str(r.get("processDate") or "")[:10])
            if day:
                r["outdoorTempMeanC"] = day.get("tempMeanC")
                r["outdoorTempMaxC"] = day.get("tempMaxC")
                r["outdoorTempMinC"] = day.get("tempMinC")
            else:
                r["outdoorTempMeanC"] = None
                r["outdoorTempMaxC"] = None
                r["outdoorTempMinC"] = None
            new_rows.append(r)
        enriched[stage] = new_rows
    return enriched


def _merge_process_into_weather_stages(
    weather_summary: dict[str, Any] | None,
    process_details: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """把工艺对照行写入 weather_summary.stages[*].processRows。"""
    if weather_summary is None:
        return None
    stages = dict(weather_summary.get("stages") or {})
    for stage, rows in process_details.items():
        label = STAGE_WEATHER_LABELS.get(stage, stage)
        block = dict(stages.get(stage) or {})
        if not block:
            block = {
                "stage": stage,
                "label": label,
                "dates": sorted({str(r.get("processDate")) for r in rows}),
                "dayCount": 0,
                "days": [],
                "display": f"{label}工艺对照 {len(rows)} 行",
            }
        block["processRows"] = rows
        block["processRowCount"] = len(rows)
        stages[stage] = block
    weather_summary = dict(weather_summary)
    weather_summary["stages"] = stages
    return weather_summary


def _decorate_process_rows_with_inventory(
    rows: list[dict[str, Any]],
    inventory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """把物料级材质/浇筑类型/冒口/部位补到每条工艺行（同型号各行相同，客户要看行内列）。"""
    if not inventory:
        return rows
    riser = inventory.get("riser") if isinstance(inventory.get("riser"), dict) else {}
    extras = {
        "materialName": inventory.get("materialTypeName"),
        "castingTypeName": inventory.get("castingTypeName")
        or inventory.get("castingTypeKey"),
        "castingTypeKey": inventory.get("castingTypeKey"),
        "riserSpec": riser.get("spec"),
        "position": inventory.get("position"),
        "spec": inventory.get("spec") or inventory.get("productSize"),
        "grossWeight": inventory.get("weightGross"),
        "riserWeight": riser.get("weightGross"),
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, val in extras.items():
            if not item.get(key) and val:
                item[key] = val
        out.append(item)
    return out


def _build_process_summary(
    *,
    inventory: dict[str, Any] | None,
    process_details: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for stage, rows in process_details.items():
        filled = _decorate_process_rows_with_inventory(rows, inventory)
        hint = 0
        if filled and filled[0].get("_truncatedHint"):
            try:
                hint = int(filled[0].pop("_truncatedHint", 0) or 0)
            except (TypeError, ValueError):
                hint = 0
            for r in filled[1:]:
                r.pop("_truncatedHint", None)
        stages[stage] = {
            "stage": stage,
            "label": STAGE_WEATHER_LABELS.get(stage, stage),
            "rowCount": len(filled),
            "rows": filled,
            "rowsTruncated": hint,
        }
    groups = []
    for grp in PROCESS_GROUPS:
        keys = list(grp["stages"])
        groups.append(
            {
                "id": grp["id"],
                "label": grp["label"],
                "stages": keys,
                "rowCount": sum(
                    int((stages.get(k) or {}).get("rowCount") or 0) for k in keys
                ),
            }
        )
    return {
        "inventory": {
            "inventoryGuid": inventory.get("inventoryGuid") if inventory else None,
            "code": inventory.get("code") if inventory else None,
            "name": inventory.get("name") if inventory else None,
            "spec": inventory.get("spec") if inventory else None,
        },
        "groups": groups,
        "stages": stages,
        "note": (
            "工艺对照按 InventoryGUID 过滤，分砂型作业 / 熔铸作业 / 加工作业。"
            "打型/粘型来自 WorkMakingObject；组型/浇铸/出箱来自 WorkCastingInBox 的"
            "InBoxDate / CastedDate / ActualOutDate；电炉=WorkCastingFurnaceItem；"
            "切冒/改型/加工日计划按在制件关联；切检=QualityCutRiserStage；"
            "实物报废=QualityObjectScrap；终检=QualityFinalStage。"
            "切冒计划主要对应浇筑类型 WS，PT 物料常无记录。"
        ),
    }


def _attach_stage_weather(
    weather_summary: dict[str, Any],
    stage_date_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """把按日气温切到各工序，写入 weather_summary['stages']。"""
    by_day = {
        str(d.get("date") or "")[:10]: d
        for d in (weather_summary.get("days") or [])
        if d.get("date")
    }
    stages: dict[str, Any] = {}
    stage_dates: dict[str, list[str]] = {}
    for row in stage_date_rows:
        stage = str(_row_get(row, "StageName", "stageName") or "").strip()
        date_s = str(_row_get(row, "ProcessDate", "processDate") or "")[:10]
        if not stage or len(date_s) < 10:
            continue
        stage_dates.setdefault(stage, []).append(date_s)

    summary_lines: list[str] = []
    for stage, dates in stage_dates.items():
        unique = sorted(set(dates))
        days = [by_day[d] for d in unique if d in by_day]
        means = [x.get("tempMeanC") for x in days if x.get("tempMeanC") is not None]
        label = STAGE_WEATHER_LABELS.get(stage, stage)
        avg = round(sum(means) / len(means), 2) if means else None
        amin = min(means) if means else None
        amax = max(means) if means else None
        # display 仅短摘要，明细走 days[]，避免前端/LLM 挤成一行
        short = (
            f"{len(days)}天，均温 {avg}℃（{amin}~{amax}℃）"
            if means
            else f"{len(days)}天，无气温数据"
        )
        stage_block = {
            "stage": stage,
            "label": label,
            "dates": unique,
            "dayCount": len(days),
            "missingCount": max(0, len(unique) - len(days)),
            "tempMeanMinC": amin,
            "tempMeanMaxC": amax,
            "tempMeanAvgC": avg,
            "days": days,
            "display": short,
        }
        stages[stage] = stage_block
        summary_lines.append(f"{label}：{short}")

    weather_summary["stages"] = stages
    if summary_lines:
        loc = (weather_summary.get("location") or {}).get("name") or "厂区"
        weather_summary["display"] = f"{loc}多工序历史气温\n" + "\n".join(summary_lines)
    return weather_summary


async def find_mssql_server(db: AsyncSession) -> McpServer:
    result = await db.execute(
        select(McpServer)
        .where(McpServer.enabled.is_(True))
        .options(selectinload(McpServer.tools))
    )
    preferred: McpServer | None = None
    for server in result.scalars().unique().all():
        tools = {t.name for t in (server.tools or []) if getattr(t, "enabled", True)}
        if "execute_query" not in tools:
            continue
        name = (server.name or "").lower()
        if "sql" in name or "mes" in name or "mssql" in name:
            return server
        if preferred is None:
            preferred = server
    if preferred is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "未找到启用且含 execute_query 的 MSSQL MCP Server，请先在 MCP 管理中配置",
            status_code=404,
        )
    return preferred


async def find_weather_mcp_server(db: AsyncSession) -> McpServer | None:
    """优先取配置了 FACTORY_LAT/LON 且含历史天气工具的 MCP。"""
    result = await db.execute(
        select(McpServer)
        .where(McpServer.enabled.is_(True))
        .options(selectinload(McpServer.tools))
    )
    fallback: McpServer | None = None
    for server in result.scalars().unique().all():
        tools = {
            t.name
            for t in (server.tools or [])
            if getattr(t, "enabled", True)
        }
        if "get_historical_weather" not in tools and "get_weather" not in tools:
            continue
        env = server.env if isinstance(server.env, dict) else {}
        if (env.get("FACTORY_LAT") or "").strip() and (env.get("FACTORY_LON") or "").strip():
            return server
        if fallback is None:
            fallback = server
    return fallback


async def resolve_factory_location(db: AsyncSession) -> dict[str, Any]:
    """厂区坐标：MCP 天气工具 env → 进程环境 → 郑州兜底。"""
    from api.services.casting.weather import (
        factory_location,
        factory_location_from_env_map,
    )

    server = await find_weather_mcp_server(db)
    if server is not None:
        loc = factory_location_from_env_map(
            server.env if isinstance(server.env, dict) else {}
        )
        if loc:
            loc["serverName"] = server.name
            logger.warning(
                "casting.weather location from mcp name=%s loc=%s",
                server.name,
                loc.get("name"),
            )
            return loc
    loc = factory_location()
    logger.warning(
        "casting.weather location fallback source=%s name=%s",
        loc.get("source"),
        loc.get("name"),
    )
    return loc


def _short_guid(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.replace("0", "").replace("-", "") == "":
        return None
    return text


def _parse_inventory_profile(row: dict[str, Any], guid: str) -> dict[str, Any]:
    size_a = _as_float(_row_get(row, "SizeA", "sizeA"))
    size_b = _as_float(_row_get(row, "SizeB", "sizeB"))
    size_h = _as_float(_row_get(row, "SizeH", "sizeH"))
    product_size = None
    if size_a is not None or size_b is not None or size_h is not None:
        product_size = "×".join(
            _fmt_num(v) if v is not None else "—" for v in (size_a, size_b, size_h)
        )
    spec = _row_get(row, "InventorySpecification", "Spec", "spec")
    return {
        "inventoryGuid": str(
            _row_get(row, "InventoryGUID", "inventoryGuid", "GUID") or guid
        ),
        "code": _row_get(row, "InventoryCode", "Code", "code"),
        "name": _row_get(row, "InventoryName", "Name", "name"),
        "spec": spec,
        "commonName": _row_get(row, "InventoryCommonName", "commonName"),
        "shape": _row_get(row, "InventoryShape", "shape"),
        "position": _row_get(row, "InventoryPosition", "position"),
        "sizeLength": _as_float(_row_get(row, "InventorySizeLength")),
        "sizeWidth": _as_float(_row_get(row, "InventorySizeWidth")),
        "sizeHeight": _as_float(_row_get(row, "InventorySizeHeight")),
        "weightGross": _as_float(_row_get(row, "InventoryWeightGross")),
        "sizeA": size_a,
        "sizeB": size_b,
        "sizeH": size_h,
        "productSize": product_size or spec,
        "volume": _as_float(_row_get(row, "Volume", "volume")),
        "cutRiserArea": _as_float(_row_get(row, "CutRiserArea")),
        "machiningArea": _as_float(_row_get(row, "MachiningArea")),
        "drillingDepth": _as_float(_row_get(row, "DrillingDepth")),
        "productAnnealingDays": _as_float(_row_get(row, "ProductAnnealingDays")),
        "materialTypeGuid": _short_guid(_row_get(row, "ProductMateralTypeGUID")),
        "materialTypeName": _row_get(row, "ProductMateralTypeName", "materialTypeName"),
        "castTypeGuid": _short_guid(_row_get(row, "ProductTypeGUID")),
        "productTypeCode": _row_get(row, "ProductTypeCode", "productTypeCode"),
        "productTypeName": _row_get(row, "ProductTypeName", "productTypeName"),
        "castingTypeKey": _row_get(row, "SCastingTypeKey", "castingTypeKey"),
        "castingTypeName": _row_get(row, "CastingTypeName", "castingTypeName"),
        "inventoryTypeGuid": _short_guid(_row_get(row, "InventoryTypeGUID")),
        "riser": {
            "inventoryGuid": _short_guid(_row_get(row, "RiserInventoryGUID")),
            "code": _row_get(row, "RiserCode", "riserCode"),
            "name": _row_get(row, "RiserName", "riserName"),
            "spec": _row_get(row, "RiserSpec", "riserSpec"),
            "weightGross": _as_float(_row_get(row, "RiserWeightGross")),
        },
    }


_TEMP_BINS: list[tuple[str, float | None, float | None]] = [
    ("<0℃", None, 0.0),
    ("0–10℃", 0.0, 10.0),
    ("10–20℃", 10.0, 20.0),
    ("20–30℃", 20.0, 30.0),
    ("≥30℃", 30.0, None),
]


def _temp_bin_label(temp: float) -> str:
    for label, lo, hi in _TEMP_BINS:
        if lo is None and temp < float(hi or 0):
            return label
        if hi is None and temp >= float(lo or 0):
            return label
        if lo is not None and hi is not None and lo <= temp < hi:
            return label
    return "其它"


def _build_weather_yield(
    rows: list[dict[str, Any]],
    *,
    basis: str,
    note: str,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, float]] = {
        label: {"inputQty": 0.0, "passQty": 0.0, "scrapQty": 0.0, "dayCount": 0.0}
        for label, _, _ in _TEMP_BINS
    }
    used = 0
    for row in rows:
        temp = row.get("outdoorTempMeanC")
        if not isinstance(temp, (int, float)):
            continue
        label = _temp_bin_label(float(temp))
        b = buckets.setdefault(
            label, {"inputQty": 0.0, "passQty": 0.0, "scrapQty": 0.0, "dayCount": 0.0}
        )
        b["inputQty"] += float(row.get("inputQty") or 0)
        b["passQty"] += float(row.get("passQty") or 0)
        b["scrapQty"] += float(row.get("scrapQty") or 0)
        b["dayCount"] += 1
        used += 1

    bins: list[dict[str, Any]] = []
    for label, _, _ in _TEMP_BINS:
        b = buckets.get(label) or {}
        inp = float(b.get("inputQty") or 0)
        pas = float(b.get("passQty") or 0)
        if inp <= 0 and int(b.get("dayCount") or 0) <= 0:
            continue
        bins.append(
            {
                "label": label,
                "dayCount": int(b.get("dayCount") or 0),
                "inputQty": inp,
                "passQty": pas,
                "scrapQty": float(b.get("scrapQty") or 0),
                "yieldRate": round(pas / inp, 4) if inp > 0 else None,
            }
        )
    ranked = [
        x
        for x in bins
        if isinstance(x.get("yieldRate"), float) and x.get("inputQty")
    ]
    best = (
        max(ranked, key=lambda x: (float(x["yieldRate"]), float(x["inputQty"])))
        if ranked
        else None
    )
    worst = (
        min(ranked, key=lambda x: (float(x["yieldRate"]), -float(x["inputQty"])))
        if ranked
        else None
    )
    return {
        "basis": basis,
        "note": note,
        "sampleDays": used,
        "bins": bins,
        "bestBin": best,
        "worstBin": worst,
    }


_COMBO_MIN_INPUT = 3.0
_COMBO_LIST_LIMIT = 20


def _parse_combo_date_row(row: dict[str, Any]) -> dict[str, Any] | None:
    date_s = str(_row_get(row, "ProcessDate", "processDate") or "")[:10]
    if len(date_s) < 10:
        return None
    inp = _as_float(_row_get(row, "InputQty", "inputQty"))
    pas = _as_float(_row_get(row, "PassQty", "passQty"))
    item: dict[str, Any] = {
        "processDate": date_s,
        "shiftCode": _row_get(row, "ShiftCode", "shiftCode"),
        "shiftName": _row_get(row, "ShiftName", "shiftName"),
        "furnaceName": _row_get(row, "FurnaceName", "furnaceName"),
        "monthFurnaceNo": _row_get(row, "MonthFurnaceNo", "monthFurnaceNo"),
        "boxNo": _row_get(row, "BoxNo", "boxNo"),
        "lotCount": _as_int(_row_get(row, "LotCount", "lotCount")),
        "inputQty": inp,
        "passQty": pas,
        "scrapQty": _as_float(_row_get(row, "ScrapQty", "scrapQty")),
        "sampleObjectCode": _row_get(row, "SampleObjectCode", "sampleObjectCode"),
    }
    if isinstance(inp, (int, float)) and inp > 0 and isinstance(pas, (int, float)):
        item["yieldRate"] = round(float(pas) / float(inp), 4)
    else:
        item["yieldRate"] = None
    return item


def _combo_group_key(row: dict[str, Any], *, with_weather: bool) -> tuple[str, ...]:
    base = (
        str(row.get("shiftCode") or ""),
        str(row.get("shiftName") or ""),
        str(row.get("furnaceName") or ""),
        str(row.get("monthFurnaceNo") or ""),
        str(row.get("boxNo") or ""),
    )
    if with_weather:
        return base + (str(row.get("weatherBin") or "未关联气温"),)
    return base


def _roll_combo_groups(
    rows: list[dict[str, Any]], *, with_weather: bool
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = _combo_group_key(row, with_weather=with_weather)
        b = buckets.get(key)
        if b is None:
            b = {
                "shiftCode": row.get("shiftCode"),
                "shiftName": row.get("shiftName"),
                "furnaceName": row.get("furnaceName"),
                "monthFurnaceNo": row.get("monthFurnaceNo"),
                "boxNo": row.get("boxNo"),
                "weatherBin": row.get("weatherBin") if with_weather else None,
                "lotCount": 0,
                "inputQty": 0.0,
                "passQty": 0.0,
                "scrapQty": 0.0,
                "dateFrom": row.get("processDate"),
                "dateTo": row.get("processDate"),
                "sampleObjectCode": row.get("sampleObjectCode"),
            }
            buckets[key] = b
        b["lotCount"] += int(row.get("lotCount") or 0)
        b["inputQty"] += float(row.get("inputQty") or 0)
        b["passQty"] += float(row.get("passQty") or 0)
        b["scrapQty"] += float(row.get("scrapQty") or 0)
        d = str(row.get("processDate") or "")[:10]
        if d:
            if not b.get("dateFrom") or d < str(b.get("dateFrom")):
                b["dateFrom"] = d
            if not b.get("dateTo") or d > str(b.get("dateTo")):
                b["dateTo"] = d
        if not b.get("sampleObjectCode") and row.get("sampleObjectCode"):
            b["sampleObjectCode"] = row.get("sampleObjectCode")
    out: list[dict[str, Any]] = []
    for b in buckets.values():
        inp = float(b.get("inputQty") or 0)
        pas = float(b.get("passQty") or 0)
        b["inputQty"] = inp
        b["passQty"] = pas
        b["scrapQty"] = float(b.get("scrapQty") or 0)
        b["yieldRate"] = round(pas / inp, 4) if inp > 0 else None
        out.append(b)
    out.sort(
        key=lambda x: (
            -(float(x["yieldRate"]) if isinstance(x.get("yieldRate"), float) else -1.0),
            -float(x.get("inputQty") or 0),
        )
    )
    return out


def _pick_best_combo(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """投入达到门槛的优先；否则退回全部里最高的，并标记样本量不足。"""
    ranked = [
        x
        for x in rows
        if isinstance(x.get("yieldRate"), float)
        and float(x.get("inputQty") or 0) >= _COMBO_MIN_INPUT
    ]
    pool = ranked or [
        x for x in rows if isinstance(x.get("yieldRate"), float)
    ]
    if not pool:
        return None, False
    best = max(
        pool, key=lambda x: (float(x["yieldRate"]), float(x.get("inputQty") or 0))
    )
    return best, bool(ranked)


def _build_combo_yield(
    date_rows: list[dict[str, Any]],
    *,
    weather_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """把浇铸日组合行滚成「班别×电炉×月炉次×箱号」及气温分箱。"""
    dated = _attach_outdoor_temp_to_process_rows(
        {"combo": date_rows}, weather_summary
    ).get("combo") or []
    for row in dated:
        temp = row.get("outdoorTempMeanC")
        row["weatherBin"] = (
            _temp_bin_label(float(temp)) if isinstance(temp, (int, float)) else None
        )
    combos = _roll_combo_groups(dated, with_weather=False)
    weather_source = [r for r in dated if r.get("weatherBin")]
    weather_combos = _roll_combo_groups(weather_source, with_weather=True)
    best, enough = _pick_best_combo(combos)
    best_weather, weather_enough = _pick_best_combo(weather_combos)
    note = (
        "本版链路：同一 MakingObjectGUID 的浇铸任务（班别/电炉/月炉次/箱号）关联最终二检。"
        "砂型未纳入组合键。气温为厂区室外均温，观察性。"
        f"推荐组合要求投入≥{_COMBO_MIN_INPUT:g}。"
    )
    return {
        "note": note,
        "linkedDayGroups": len(dated),
        "comboCount": len(combos),
        "bestCombo": best,
        "bestComboSampleOk": enough,
        "combos": combos[:_COMBO_LIST_LIMIT],
        "combosTruncated": max(0, len(combos) - _COMBO_LIST_LIMIT),
        "bestWeatherCombo": best_weather,
        "bestWeatherSampleOk": weather_enough,
        "weatherCombos": weather_combos[:_COMBO_LIST_LIMIT],
        "weatherCombosTruncated": max(0, len(weather_combos) - _COMBO_LIST_LIMIT),
    }


def _filesystem_root_from_server(server: McpServer) -> str | None:
    """从 MCP args 末尾解析允许目录（如 E:/download）。"""
    args = server.args if isinstance(getattr(server, "args", None), list) else []
    for raw in reversed(args):
        text = str(raw or "").strip().strip('"').strip("'")
        if not text or text.startswith("-") or text.startswith("@"):
            continue
        # 包名不是路径
        if text.lower().endswith((".js", ".mjs", ".cjs", ".ts")):
            continue
        if "/" in text or "\\" in text or (len(text) >= 2 and text[1] == ":"):
            return text.replace("/", "\\") if "\\" in text or ":" in text else text
    return None


def _safe_export_filename(*, name: str, code: str = "") -> str:
    """导出文件名：{物料名称}_良率分析.md（Windows 非法字符替换）。"""

    def scrub(s: str) -> str:
        invalid = '<>:"/\\|?*'
        out: list[str] = []
        for ch in (s or "").strip():
            if ch in invalid or ord(ch) < 32:
                out.append("_")
            else:
                out.append(ch)
        text = "".join(out).strip(" .")
        while "__" in text:
            text = text.replace("__", "_")
        return text[:80] or ""

    title = scrub(name) or scrub(code) or "未命名物料"
    return f"{title}_良率分析.md"


async def find_filesystem_server(db: AsyncSession) -> McpServer:
    """查找启用且含 write_file 的 filesystem MCP。"""
    result = await db.execute(
        select(McpServer)
        .where(McpServer.enabled.is_(True))
        .options(selectinload(McpServer.tools))
    )
    preferred: McpServer | None = None
    for server in result.scalars().unique().all():
        tools = {t.name for t in (server.tools or []) if getattr(t, "enabled", True)}
        if "write_file" not in tools:
            continue
        name = (server.name or "").lower()
        if "file" in name or "filesystem" in name or "fs" == name:
            return server
        if preferred is None:
            preferred = server
    if preferred is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "未找到启用且含 write_file 的 filesystem MCP Server，"
            "请先在工具管理中配置（如 npx @modelcontextprotocol/server-filesystem E:/download）",
            status_code=404,
        )
    return preferred


async def export_markdown_via_filesystem(
    db: AsyncSession,
    *,
    markdown: str,
    inventory_guid: str,
    inventory_name: str | None = None,
    inventory_code: str | None = None,
) -> dict[str, Any]:
    """把 Markdown 写到 filesystem MCP 允许目录。"""
    from pathlib import Path

    text = (markdown or "").strip()
    if not text:
        raise AppError(ErrorCode.VALIDATION, "markdown empty", status_code=422)

    server = await find_filesystem_server(db)
    root = _filesystem_root_from_server(server)
    if not root:
        raise AppError(
            ErrorCode.VALIDATION,
            "filesystem MCP 未配置允许目录（args 中需含如 E:/download）",
            status_code=422,
        )

    filename = _safe_export_filename(
        name=str(inventory_name or ""),
        code=str(inventory_code or ""),
    )
    abs_path = str(Path(root) / filename)
    # 规范化分隔符，便于 Windows filesystem MCP 校验
    abs_path = abs_path.replace("/", "\\") if ":" in abs_path[:3] else abs_path

    # 优先本机直写，避开 Windows 下 filesystem stdio MCP 关闭卡住 / 空 JSON
    try:
        out = Path(abs_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {
            "ok": True,
            "path": abs_path,
            "filename": filename,
            "root": root,
            "serverId": server.public_id,
            "serverName": server.name,
            "message": f"已写入 {abs_path}",
            "via": "local",
        }
    except OSError as exc:
        logger.warning("casting.yield local write failed, fallback MCP: %s", exc)

    result = await mcp_servers.call_tool_on_server(
        db,
        server_public_id=server.public_id,
        tool_name="write_file",
        arguments={"path": abs_path, "content": text},
        timeout_seconds=60.0,
    )
    if result.get("isError"):
        raise AppError(
            ErrorCode.INTERNAL,
            f"写入本地文件失败：{result.get('content')}",
            status_code=502,
        )
    return {
        "ok": True,
        "path": abs_path,
        "filename": filename,
        "root": root,
        "serverId": server.public_id,
        "serverName": server.name,
        "message": f"已写入 {abs_path}",
        "mcpContent": result.get("content"),
    }


async def execute_mes_query(
    db: AsyncSession,
    *,
    sql: str,
    server: McpServer | None = None,
    timeout_seconds: float = 120.0,
    pool: Any | None = None,
) -> dict[str, Any]:
    srv = server or await find_mssql_server(db)
    result = await mcp_servers.call_tool_on_server(
        db,
        server_public_id=srv.public_id,
        tool_name="execute_query",
        arguments={"query": sql},
        timeout_seconds=timeout_seconds,
        pool=pool,
    )
    if result.get("isError"):
        raise AppError(
            ErrorCode.INTERNAL,
            f"MES 查询失败：{result.get('content')}",
            status_code=502,
        )
    content = result.get("content")
    rows = _parse_mcp_rows(content)
    return {
        "serverId": srv.public_id,
        "serverName": srv.name,
        "rows": rows,
        "raw": content,
    }


def _row_get(row: dict[str, Any], *keys: str) -> Any:
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        if key.lower() in lower_map:
            return lower_map[key.lower()]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    if f is None:
        return None
    return int(f)


async def analyze_yield(
    db: AsyncSession,
    *,
    inventory_guid: str | None = None,
    query: str | None = None,
    include_weather: bool = False,
    progress: ProgressCb = None,
    order_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 GUID 或名称/编码查询分析；多命中时返回 needSelect+candidates。"""
    await _emit_progress(progress, step="match", label="正在匹配物料", status="running")
    resolved = await resolve_inventory_query(
        db, inventory_guid=inventory_guid, query=query
    )
    status = resolved.get("status")
    q = resolved.get("query")
    if status == "not_found":
        return {
            "found": False,
            "needSelect": False,
            "message": resolved.get("message") or "没有该型号的历史订单/物料档案",
            "inventoryGuid": "",
            "inventory": None,
            "lines": [],
            "bestLine": None,
            "productionCount": 0,
            "weatherSummary": None,
            "rawContext": "",
            "warnings": [],
            "candidates": [],
            "query": q,
        }
    if status == "candidates":
        return {
            "found": False,
            "needSelect": True,
            "message": resolved.get("message") or "请选择物料后继续分析",
            "inventoryGuid": "",
            "inventory": None,
            "lines": [],
            "bestLine": None,
            "productionCount": 0,
            "weatherSummary": None,
            "rawContext": "",
            "warnings": [],
            "candidates": resolved.get("candidates") or [],
            "query": q,
        }
    guid = str(resolved.get("guid") or "").strip()
    await _emit_progress(progress, step="match", label="正在匹配物料", status="done")
    data = await analyze_yield_by_inventory_guid(
        db,
        inventory_guid=guid,
        include_weather=include_weather,
        progress=progress,
        order_context=order_context,
    )
    data["needSelect"] = False
    data["candidates"] = []
    data["query"] = q
    return data


async def analyze_yield_by_inventory_guid(
    db: AsyncSession,
    *,
    inventory_guid: str,
    include_weather: bool = False,
    progress: ProgressCb = None,
    order_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 InventoryGUID 分析：无档案 → found=false；有则汇总产线良率。"""
    from api.services.mcp.isolated_stdio import (
        run_execute_queries_isolated,
        snapshot_mcp_server,
    )

    guid = (inventory_guid or "").strip()
    if not guid:
        raise AppError(ErrorCode.VALIDATION, "inventoryGuid required", status_code=422)

    t0 = time.perf_counter()
    logger.warning(
        "casting.yield start guid=%s include_weather=%s",
        guid,
        include_weather,
    )
    factory_loc = await resolve_factory_location(db)
    server = await find_mssql_server(db)
    snap = snapshot_mcp_server(server)

    steps: list[tuple[str, str]] = [
        ("inventory", sql_inventory_by_guid(guid)),
        ("stock", sql_finished_stock(guid)),
        ("stock_all", sql_all_stock(guid)),
        ("yield_by_line", sql_yield_by_line(guid)),
        ("yield_by_cast_date", sql_yield_by_cast_date(guid)),
        ("yield_by_combo_date", sql_yield_by_combo_date(guid)),
        ("smelt_electrical", sql_smelt_electrical(guid)),
    ]
    # 工序工艺对照（含 ProcessDate，可兼作气温日期源）
    steps.extend(sql_process_stage_details_all(guid))

    await _emit_progress(
        progress, step="mes", label="正在查库存 / 良率 / 工序", status="running"
    )
    logger.warning(
        "casting.yield mes_batch start guid=%s steps=%s",
        guid,
        [s for s, _ in steps],
    )
    try:
        batch = await asyncio.to_thread(
            run_execute_queries_isolated,
            server_snapshot=snap,
            steps=steps,
            timeout_seconds=120.0,
        )
    except AppError:
        logger.exception(
            "casting.yield mes_batch failed guid=%s elapsed=%.1fs",
            guid,
            time.perf_counter() - t0,
        )
        raise
    except Exception as exc:
        logger.exception(
            "casting.yield mes_batch failed guid=%s elapsed=%.1fs",
            guid,
            time.perf_counter() - t0,
        )
        raise AppError(
            ErrorCode.INTERNAL,
            f"MES 批量查询失败：{exc}",
            status_code=502,
        ) from exc

    logger.warning(
        "casting.yield mes_batch ok guid=%s elapsed=%.1fs",
        guid,
        time.perf_counter() - t0,
    )
    await _emit_progress(
        progress, step="mes", label="正在查库存 / 良率 / 工序", status="done"
    )

    # 结构化解析
    for step, payload in batch.items():
        payload["rows"] = _parse_mcp_rows(payload.get("content") or payload.get("raw"))

    try:
        result = await _analyze_yield_from_batch(
            guid=guid,
            server_name=str(snap.get("name") or ""),
            server_id=str(snap.get("publicId") or ""),
            batch=batch,
            include_weather=include_weather,
            factory_location=factory_loc,
            progress=progress,
            order_context=order_context,
        )
        logger.warning(
            "casting.yield business_done guid=%s found=%s lines=%s elapsed=%.1fs",
            guid,
            result.get("found"),
            len(result.get("lines") or []),
            time.perf_counter() - t0,
        )
        logger.warning(
            "casting.yield returning guid=%s total=%.1fs",
            guid,
            time.perf_counter() - t0,
        )
        return result
    except Exception:
        logger.exception(
            "casting.yield failed guid=%s elapsed=%.1fs",
            guid,
            time.perf_counter() - t0,
        )
        raise


async def _analyze_yield_from_batch(
    *,
    guid: str,
    server_name: str,
    server_id: str,
    batch: dict[str, dict[str, Any]],
    include_weather: bool,
    factory_location: dict[str, Any] | None = None,
    progress: ProgressCb = None,
    order_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inv = batch.get("inventory") or {"rows": [], "raw": None}
    inv_rows = inv.get("rows") or []
    if not inv_rows:
        raw_text = str(inv.get("raw") or "")
        looks_empty = (
            not raw_text.strip()
            or "0 rows" in raw_text.lower()
            or "[]" in raw_text.replace(" ", "")
            or "no rows" in raw_text.lower()
            or '"rowCount": 0' in raw_text.replace(" ", "")
            or '"rowCount":0' in raw_text.replace(" ", "")
        )
        if looks_empty or not raw_text.strip():
            return {
                "found": False,
                "message": "没有该型号的历史订单/物料档案",
                "inventoryGuid": guid,
                "inventory": None,
                "lines": [],
                "bestLine": None,
                "productionCount": 0,
                "rawContext": "",
                "warnings": [
                    "未在 invn.Inventory 命中 InventoryGUID；"
                    "若列名不同请调整 services/casting/yield_analysis.py 中 SQL"
                ],
                "mesRaw": {"inventory": inv.get("raw")},
            }
        inventory = {
            "inventoryGuid": guid,
            "code": None,
            "name": None,
            "spec": None,
            "raw": inv.get("raw"),
        }
    else:
        inventory = _parse_inventory_profile(inv_rows[0], guid)

    warnings: list[str] = []

    # 成品库存（先于良率展示 / 写入上下文）
    def _parse_stock_rows(block: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in block.get("rows") or []:
            out.append(
                {
                    "warehouseCode": _row_get(row, "WarehouseCode", "warehouseCode"),
                    "warehouseName": _row_get(row, "WarehouseName", "warehouseName"),
                    "availableQty": _as_float(_row_get(row, "AvailableQty", "availableQty")),
                    "lockedQty": _as_float(_row_get(row, "LockedQty", "lockedQty")),
                    "inTransitQty": _as_float(
                        _row_get(row, "InTransitQty", "inTransitQty")
                    ),
                    "totalQty": _as_float(_row_get(row, "TotalQty", "totalQty")),
                }
            )
        return out

    def _stock_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
        return {
            "availableQty": sum(float(x.get("availableQty") or 0) for x in rows),
            "lockedQty": sum(float(x.get("lockedQty") or 0) for x in rows),
            "inTransitQty": sum(float(x.get("inTransitQty") or 0) for x in rows),
            "totalQty": sum(float(x.get("totalQty") or 0) for x in rows),
        }

    stock_q = batch.get("stock") or {"rows": [], "raw": None}
    stock_all_q = batch.get("stock_all") or {"rows": [], "raw": None}
    stock_warehouses = _parse_stock_rows(stock_q)
    stock_all_warehouses = _parse_stock_rows(stock_all_q)
    finished_codes = {
        str(x.get("warehouseCode") or "").strip().upper()
        for x in stock_warehouses
        if x.get("warehouseCode")
    }
    for wh in stock_all_warehouses:
        code = str(wh.get("warehouseCode") or "").strip().upper()
        name = str(wh.get("warehouseName") or "")
        is_finished = code in finished_codes or (
            "成品" in name and "半成品" not in name and "废品" not in name
        )
        wh["category"] = "成品" if is_finished else "其它"
        wh["isFinished"] = is_finished
    if not stock_warehouses and not stock_all_warehouses and stock_q.get("raw"):
        warnings.append("成品库存 SQL 已执行但未能结构化解析（invn.Stock）")
    fin = _stock_totals(stock_warehouses)
    all_tot = _stock_totals(stock_all_warehouses)
    stock_summary: dict[str, Any] = {
        "availableQty": fin["availableQty"],
        "lockedQty": fin["lockedQty"],
        "inTransitQty": fin["inTransitQty"],
        "totalQty": fin["totalQty"],
        "warehouseCount": len(stock_warehouses),
        "warehouses": stock_warehouses,
        "allAvailableQty": all_tot["availableQty"],
        "allLockedQty": all_tot["lockedQty"],
        "allInTransitQty": all_tot["inTransitQty"],
        "allTotalQty": all_tot["totalQty"],
        "allWarehouseCount": len(stock_all_warehouses),
        "allWarehouses": stock_all_warehouses,
        "note": (
            "全仓明细来自 invn.Stock（Available≈UUStockQty）；"
            "类型「成品」表示仓库名含成品且排除半成品/废品，或编码 A00。"
        ),
    }

    yield_q = batch.get("yield_by_line") or {"rows": [], "raw": None}
    lines: list[dict[str, Any]] = []
    for row in yield_q.get("rows") or []:
        rate = _as_float(_row_get(row, "YieldRate", "yieldRate"))
        lines.append(
            {
                "lineCode": _row_get(row, "LineCode", "lineCode"),
                "lineName": _row_get(row, "LineName", "lineName"),
                "lotCount": _as_int(_row_get(row, "LotCount", "lotCount")),
                "inputQty": _as_float(_row_get(row, "InputQty", "inputQty")),
                "passQty": _as_float(_row_get(row, "PassQty", "passQty")),
                "scrapQty": _as_float(_row_get(row, "ScrapQty", "scrapQty")),
                "yieldRate": rate,
            }
        )

    if not lines and yield_q.get("raw"):
        warnings.append(
            "良率 SQL 已执行但未能解析为结构化行；请检查 QualitySecondStage 关联"
            "（InventoryMakingObject / WorkGroup）或查看 mesRaw.yield"
        )

    best_line = lines[0] if lines else None
    production_count = sum(int(x.get("lotCount") or 0) for x in lines)

    if inventory and not lines:
        warnings.append("物料档案存在，但未汇总到二检产线良率数据")

    # 工序工艺对照（始终尝试解析）
    process_details_raw = _collect_process_details_from_batch(batch)
    for stage, rows in process_details_raw.items():
        block = batch.get(f"detail_{stage}") or {}
        if not rows and block.get("raw"):
            warnings.append(
                f"{STAGE_WEATHER_LABELS.get(stage, stage)}工艺明细未能结构化解析"
            )

    smelt_electrical = _parse_smelt_electrical_from_batch(batch)

    weather_summary = None
    if include_weather:
        await _emit_progress(
            progress, step="weather", label="正在关联历史气温", status="running"
        )
        try:
            stage_rows: list[dict[str, Any]] = []
            for stage, rows in process_details_raw.items():
                for r in rows:
                    stage_rows.append(
                        {
                            "StageName": stage,
                            "ProcessDate": r.get("processDate"),
                        }
                    )
            dates: list[str] = []
            for row in stage_rows:
                d = _row_get(row, "ProcessDate", "processDate", "InspectDate")
                if d:
                    dates.append(str(d)[:10])
            from api.services.casting.weather import fetch_historical_weather

            tw = time.perf_counter()
            logger.warning(
                "casting.yield step=weather start guid=%s unique_dates=%s loc=%s",
                guid,
                len(set(dates)),
                (factory_location or {}).get("name"),
            )
            weather_summary = await asyncio.to_thread(
                fetch_historical_weather,
                dates=dates,
                location=factory_location,
            )
            logger.warning(
                "casting.yield step=weather ok guid=%s dayCount=%s elapsed=%.1fs",
                guid,
                (weather_summary.get("summary") or {}).get("dayCount"),
                time.perf_counter() - tw,
            )
            if stage_rows:
                weather_summary = _attach_stage_weather(weather_summary, stage_rows)
            if weather_summary.get("error"):
                warnings.append(f"天气查询异常：{weather_summary.get('error')}")
            elif not dates:
                warnings.append("无工序日期，跳过历史天气关联")
            else:
                missing_stages = [
                    label
                    for key, label in STAGE_WEATHER_LABELS.items()
                    if key not in (weather_summary.get("stages") or {})
                ]
                if missing_stages:
                    warnings.append(
                        "以下工序未命中日期（气温仅覆盖有日期的工序）："
                        + "、".join(missing_stages)
                    )
            loc_src = (weather_summary.get("location") or {}).get("source")
            loc_name = (weather_summary.get("location") or {}).get("name")
            if loc_src == "default":
                warnings.append(
                    "未读到 MCP 天气工具的 FACTORY_LAT/LON，已用郑州示例坐标；"
                    "请在「通用工具（时间/天气）」环境变量中配置厂区"
                )
            elif loc_name:
                warnings.append(f"历史气温厂区：{loc_name}（来自天气 MCP 配置）")
        except Exception as exc:  # noqa: BLE001
            logger.exception("include_weather failed")
            warnings.append(f"天气汇总失败：{exc}")
            weather_summary = {"error": str(exc), "days": [], "summary": {}, "stages": {}}
        await _emit_progress(
            progress, step="weather", label="正在关联历史气温", status="done"
        )

    process_details = _attach_outdoor_temp_to_process_rows(
        process_details_raw, weather_summary
    )
    process_summary = _build_process_summary(
        inventory=inventory, process_details=process_details
    )

    await _emit_progress(
        progress, step="correlate", label="正在对照气温与良率", status="running"
    )
    cast_date_rows: list[dict[str, Any]] = []
    for row in (batch.get("yield_by_cast_date") or {}).get("rows") or []:
        parsed = _parse_process_detail_row({**row, "StageName": "cast_yield"})
        if parsed:
            cast_date_rows.append(parsed)
    cast_date_rows = _attach_outdoor_temp_to_process_rows(
        {"cast_yield": cast_date_rows}, weather_summary
    ).get("cast_yield") or []

    inspect_rows = list((process_details.get("second_inspect") or []))
    weather_yield = {
        "castingDay": _build_weather_yield(
            cast_date_rows,
            basis="casting_day",
            note="按浇铸日期关联厂区室外均温，良率为该在制件最终二检结果（更贴近浇铸环境）。",
        ),
        "inspectDay": _build_weather_yield(
            inspect_rows,
            basis="inspect_day",
            note="按二检录入日关联室外均温（观察性，检验日未必等于浇铸日）。",
        ),
    }

    combo_date_rows: list[dict[str, Any]] = []
    combo_block = batch.get("yield_by_combo_date") or {}
    for row in combo_block.get("rows") or []:
        parsed = _parse_combo_date_row(row)
        if parsed:
            combo_date_rows.append(parsed)
    if not combo_date_rows and combo_block.get("raw"):
        warnings.append("浇铸→二检组合 SQL 已执行但未能结构化解析")
    combo_yield = _build_combo_yield(
        combo_date_rows, weather_summary=weather_summary
    )
    if not combo_yield.get("bestCombo"):
        warnings.append(
            "未能从浇铸任务关联到二检组合良率（该型号可能缺少 CastedDate 或二检）"
        )
    elif not combo_yield.get("bestComboSampleOk"):
        warnings.append(
            f"组合良率最高行投入不足 {_COMBO_MIN_INPUT:g}，仅供观察、不得当绝对最优"
        )

    await _emit_progress(
        progress, step="correlate", label="正在对照气温与良率", status="done"
    )

    if weather_summary is not None:
        weather_summary = _merge_process_into_weather_stages(
            weather_summary, process_details
        )
    elif any(process_details.values()):
        # 无天气时也构造精简 stages，方便前端统一读 processRows
        weather_summary = _merge_process_into_weather_stages(
            {
                "days": [],
                "summary": {},
                "stages": {},
                "display": "工序工艺对照（未拉取室外气温）",
            },
            process_details,
        )

    logger.warning("casting.yield step=build_context start guid=%s", guid)
    raw_context = _build_raw_context(
        inventory_guid=guid,
        inventory=inventory,
        lines=lines,
        best_line=best_line,
        warnings=warnings,
        weather_summary=weather_summary,
        process_summary=process_summary,
        stock_summary=stock_summary,
        weather_yield=weather_yield,
        combo_yield=combo_yield,
        order_context=order_context,
    )
    insights = _build_query_insights(
        inventory_guid=guid,
        inventory=inventory,
        stock_summary=stock_summary,
        lines=lines,
        best_line=best_line,
        combo_yield=combo_yield,
        process_details=process_details,
        weather_yield=weather_yield,
        warnings=warnings,
        order_context=order_context,
    )
    if weather_summary:
        weather_summary = _slim_weather_for_response(weather_summary)
    process_summary = _slim_process_summary(process_summary)

    return {
        "found": True,
        "message": "ok",
        "inventoryGuid": guid,
        "inventory": inventory,
        "stockSummary": stock_summary,
        "lines": lines,
        "bestLine": best_line,
        "productionCount": production_count,
        "comboYield": combo_yield,
        "weatherSummary": weather_summary,
        "weatherYield": weather_yield,
        "processSummary": process_summary,
        "smeltElectrical": smelt_electrical,
        "rawContext": raw_context,
        "orderContext": _normalize_order_context(order_context),
        "insights": insights,
        "warnings": warnings,
        "mesRaw": {
            "inventory": inv.get("raw") if not inv_rows else None,
            "yield": yield_q.get("raw") if not (yield_q.get("rows") or []) else None,
            "stock": stock_q.get("raw") if not stock_warehouses else None,
            "serverId": server_id,
            "serverName": server_name,
        },
        "sqlTemplates": {
            "inventory": sql_inventory_by_guid("{InventoryGUID}"),
            "stock": sql_finished_stock("{InventoryGUID}"),
            "yieldByLine": sql_yield_by_line("{InventoryGUID}"),
        },
    }


def _slim_weather_for_response(weather_summary: dict[str, Any]) -> dict[str, Any]:
    """压缩顶层 days；工序 days / processRows 保留较多供前端表格展开。"""
    out = dict(weather_summary)
    days = list(out.get("days") or [])
    out["days"] = days[:12]
    out["daysTruncated"] = max(0, len(days) - 12)
    stages = out.get("stages")
    if isinstance(stages, dict):
        slim_stages: dict[str, Any] = {}
        for key, block in stages.items():
            if not isinstance(block, dict):
                slim_stages[key] = block
                continue
            b = dict(block)
            stage_days = list(b.get("days") or [])
            # 前端按工序展开明细，保留更多天；过长再截断
            b["days"] = stage_days[:24]
            b["daysTruncated"] = max(0, len(stage_days) - 24)
            dates = list(b.get("dates") or [])
            if len(dates) > 40:
                b["dates"] = dates[:40]
                b["datesTruncated"] = len(dates) - 40
            proc_rows = list(b.get("processRows") or [])
            b["processRows"] = proc_rows[:24]
            b["processRowsTruncated"] = max(0, len(proc_rows) - 24)
            slim_stages[key] = b
        out["stages"] = slim_stages
    return out


def _slim_process_summary(process_summary: dict[str, Any]) -> dict[str, Any]:
    out = dict(process_summary)
    stages = out.get("stages")
    if isinstance(stages, dict):
        slim: dict[str, Any] = {}
        for key, block in stages.items():
            if not isinstance(block, dict):
                slim[key] = block
                continue
            b = dict(block)
            rows = list(b.get("rows") or [])
            b["rows"] = rows[:24]
            b["rowsTruncated"] = max(0, len(rows) - 24)
            slim[key] = b
        out["stages"] = slim
    return out


def _fmt_num(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-6:
            return str(int(round(v)))
        return f"{v:.1f}"
    return str(v)


def _fmt_weight(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        text = f"{v:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(v)


_PROCESS_MD_ROW_LIMIT = 40


def _append_process_markdown(
    parts: list[str], key: str, rows: list[dict[str, Any]]
) -> None:
    """把单工序明细写成 Markdown 表（导出用，尽量给全量上限内的行）。"""
    limit = _PROCESS_MD_ROW_LIMIT
    shown = rows[:limit]
    if key == "sand":
        parts.append(
            "| 日期 | 室外均温℃ | 班别 | 材质 | 浇筑类型 | 冒口规格 | 配给量 | 配给重量 | 完工量 | 计划号 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {r.get('materialName') or '—'} | "
                f"{r.get('castingTypeName') or r.get('castingTypeKey') or '—'} | "
                f"{r.get('riserSpec') or '—'} | {_fmt_num(r.get('allottedQty'))} | "
                f"{_fmt_num(r.get('allottedWeight'))} | {_fmt_num(r.get('finishedQty'))} | "
                f"{r.get('samplePlanCode') or '—'} |"
            )
    elif key in ("sand_build", "sand_paste"):
        parts.append(
            "| 日期 | 室外均温℃ | 班组 | 完工件数 | 材质 | 浇筑类型 | 冒口规格 | 部位 | 区域 | 在制件样例 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {_fmt_num(r.get('finishedQty'))} | "
                f"{r.get('materialName') or '—'} | "
                f"{r.get('castingTypeName') or r.get('castingTypeKey') or '—'} | "
                f"{r.get('riserSpec') or '—'} | {r.get('position') or '—'} | "
                f"{r.get('areaName') or '—'} | {r.get('sampleObjectCode') or '—'} |"
            )
    elif key == "furnace":
        parts.append(
            "| 日期 | 室外均温℃ | 班组/班别 | 材质 | 件数 | 完工 | 计划号/炉次 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {r.get('materialName') or '—'} | "
                f"{_fmt_num(r.get('lotCount'))} | {_fmt_num(r.get('finishedQty'))} | "
                f"{r.get('samplePlanCode') or r.get('monthFurnaceNo') or '—'} |"
            )
    elif key == "casting_plan":
        parts.append(
            "| 日期 | 室外均温℃ | 班组/班别 | 电炉 | 炉次 | 件数 | 完工 | 计划号 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {r.get('furnaceName') or r.get('areaName') or '—'} | "
                f"{r.get('monthFurnaceNo') or '—'} | {_fmt_num(r.get('lotCount'))} | "
                f"{_fmt_num(r.get('finishedQty'))} | {r.get('samplePlanCode') or '—'} |"
            )
    elif key == "inbox":
        parts.append(
            "| 日期 | 室外均温℃ | 班组/班别 | 炉次 | 电炉 | 部位 | 冒重 | 毛重 | 冒口规格 | 件数 | 完工 | 在制件样例 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {r.get('monthFurnaceNo') or '—'} | "
                f"{r.get('furnaceName') or '—'} | {r.get('position') or '—'} | "
                f"{_fmt_weight(r.get('riserWeight'))} | {_fmt_weight(r.get('grossWeight'))} | "
                f"{r.get('riserSpec') or '—'} | {_fmt_num(r.get('lotCount'))} | "
                f"{_fmt_num(r.get('finishedQty'))} | {r.get('sampleObjectCode') or '—'} |"
            )
    elif key == "outbox":
        parts.append(
            "| 日期 | 室外均温℃ | 班组/班别 | 件数 | 完工 | 保温天数 | 在制件样例 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            code = r.get("samplePlanCode") or r.get("sampleObjectCode") or r.get("areaName") or "—"
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {_fmt_num(r.get('lotCount'))} | "
                f"{_fmt_num(r.get('finishedQty'))} | {_fmt_num(r.get('annealingDaysAvg'))} | "
                f"{code} |"
            )
    elif key in (
        "cut_riser",
        "modify",
        "machining_daily",
        "machining_task",
    ):
        extra = "炉号/计划号" if key in ("cut_riser", "machining_daily") else "在制件样例"
        parts.append(
            f"| 日期 | 室外均温℃ | 班组/班别 | 件数 | 完工 | {extra} |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- |")
        for r in shown:
            code = r.get("samplePlanCode") or r.get("sampleObjectCode") or r.get("areaName") or "—"
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {_fmt_num(r.get('lotCount'))} | "
                f"{_fmt_num(r.get('finishedQty'))} | {code} |"
            )
    elif key == "casting":
        parts.append(
            "| 日期 | 室外均温℃ | 月炉次 | 箱号 | 班炉次 | 班别 | 电炉 | 节点 | 规格型号 | 部位 | 浇铸类型 | 材质 | 炉温 | 浇铸温 | 保温天数 | 序列号 |"
        )
        parts.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for r in shown:
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('monthFurnaceNo') or r.get('samplePlanCode') or '—'} | "
                f"{r.get('boxNo') or '—'} | {r.get('shiftFurnaceNo') or '—'} | "
                f"{r.get('lineName') or '—'} | {r.get('furnaceName') or '—'} | "
                f"{r.get('nodeCode') or '—'} | {r.get('spec') or '—'} | "
                f"{r.get('position') or '—'} | "
                f"{r.get('castingTypeName') or r.get('castingTypeKey') or '—'} | "
                f"{r.get('materialName') or '—'} | "
                f"{_fmt_num(r.get('furnaceTmpAvg'))} | {_fmt_num(r.get('castingTmpAvg'))} | "
                f"{_fmt_num(r.get('annealingDaysAvg'))} | "
                f"{r.get('sampleObjectCode') or '—'} |"
            )
    else:
        parts.append(
            "| 日期 | 室外均温℃ | 班组 | 批次 | 投入 | 合格 | 报废 | 分日良率 | 缺陷样例 |"
        )
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in shown:
            rate = r.get("yieldRate")
            rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
            parts.append(
                f"| {r.get('processDate')} | {_fmt_num(r.get('outdoorTempMeanC'))} | "
                f"{r.get('lineName') or '—'} | {_fmt_num(r.get('lotCount'))} | "
                f"{_fmt_num(r.get('inputQty'))} | {_fmt_num(r.get('passQty'))} | "
                f"{_fmt_num(r.get('scrapQty'))} | {rate_s} | "
                f"{r.get('sampleCauseDesc') or '—'} |"
            )
    rest = len(rows) - len(shown)
    if rest > 0:
        parts.append(f"- …另有 {rest} 行省略")


_ORDER_CONTEXT_KEYS = (
    "saleOrderCode",
    "saleOrderGuid",
    "saleOrderDate",
    "inventoryGuid",
    "code",
    "name",
    "spec",
    "materialName",
    "position",
    "billOrderQty",
    "scheduOrderQty",
    "workingQty",
    "stockQty",
    "mpsingQty",
)

_NARRATIVE_SYSTEM = """你是铸造 MES 工艺与质量分析助手。用户消息里已有完整的结构化分析（含表格）。
你只写解读，禁止改写、摘抄或重新生成任何表格，禁止编造数字。
只输出这四个小节（标题必须原样）：
## 1. 结论摘要
## 8. 推荐生产安排
## 9. 主要缺陷与预防
## 10. 风险与待确认项
规则：
- 结论必须点明本次查询的订单编号与本行物料；并写明下方良率/工序是该 InventoryGUID 的同型号历史数据，不是仅本订单。
- 优先引用「组合良率」推荐组合（班别×电炉×月炉次×箱号）；班组二检排名仅作对照。
- 样本不足、数据不足处明确写出；气温分箱不得写成因果关系。
- 不要输出物料档案、库存表、工序表、气温分箱表。
"""

_GEN_INSTRUCTION_MARK = "请仅依据以上数据生成"


def _normalize_order_context(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in _ORDER_CONTEXT_KEYS:
        val = raw.get(key)
        if val is None or val == "":
            continue
        if key.endswith("Qty"):
            num = _as_float(val)
            if num is not None:
                out[key] = num
            continue
        text = str(val).strip()
        if text:
            out[key] = text
    return out or None


def _order_context_lines(order: dict[str, Any]) -> list[str]:
    return [
        "## 本次查询订单",
        "以下为本页查询的销售订单本行数量；其后良率、工序、气温均为该物料 InventoryGUID 的同型号历史数据，不是仅本订单。",
        f"- 订单编号: {order.get('saleOrderCode') or '数据不足'}",
        f"- 订单日期: {order.get('saleOrderDate') or '—'}",
        f"- 本行物料: {order.get('code') or '—'} / {order.get('name') or '—'} / {order.get('spec') or '—'}",
        f"- 部位: {order.get('position') or '—'}；材质: {order.get('materialName') or '—'}",
        f"- 合同数量: {_fmt_num(order.get('billOrderQty'))}；计划数量: {_fmt_num(order.get('scheduOrderQty'))}；"
        f"本单在制: {_fmt_num(order.get('workingQty'))}；本单库存: {_fmt_num(order.get('stockQty'))}；"
        f"已排产: {_fmt_num(order.get('mpsingQty'))}",
    ]


def _ensure_order_in_raw_context(raw: str, order: dict[str, Any] | None) -> str:
    text = (raw or "").strip()
    if not order:
        return text
    block = "\n".join(_order_context_lines(order)).strip()
    if "## 本次查询订单" in text:
        return text
    if not text:
        return block
    return f"{block}\n\n{text}"


def _factual_markdown_from_context(raw: str) -> str:
    text = (raw or "").strip()
    idx = text.rfind(_GEN_INSTRUCTION_MARK)
    if idx >= 0:
        text = text[:idx].rstrip()
    return text


def _extract_narrative_sections(markdown: str) -> str:
    """只保留结论 / 推荐 / 缺陷 / 风险四个小节，丢掉模型误写的表格。"""
    text = (markdown or "").strip()
    if not text:
        return ""
    wanted = ("1.", "8.", "9.", "10.")
    chunks: list[str] = []
    current: list[str] = []
    keep = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current and keep:
                chunks.append("\n".join(current).rstrip())
            current = [line]
            title = stripped[3:].strip()
            keep = any(title.startswith(prefix) for prefix in wanted)
            continue
        if current:
            current.append(line)
    if current and keep:
        chunks.append("\n".join(current).rstrip())
    return "\n\n".join(chunks).strip()


def _warehouse_kind(wh: dict[str, Any]) -> str:
    name = str(wh.get("warehouseName") or "")
    cat = str(wh.get("category") or "")
    blob = f"{name}{cat}"
    if "废" in blob:
        return "废品"
    if "半成品" in blob:
        return "半成品"
    if "成品" in blob or cat == "成品":
        return "成品"
    return cat or "其它"


def _defect_prevention(desc: str) -> str:
    text = desc or ""
    hints: list[str] = []
    if any(k in text for k in ("碰", "掉角", "磕", "碰掉")):
        hints.append("加强开箱、浇口切除与转运防护，避免底面磕碰")
    if any(k in text for k in ("裂", "通裂", "裂纹")):
        hints.append("关注出箱时机与冷却均匀，避免局部急冷导致通裂")
    if any(k in text for k in ("夹砂", "砂眼", "气孔")):
        hints.append("检查砂型紧实与浇注排气")
    if any(k in text for k in ("缩孔", "缩松")):
        hints.append("核对冒口规格与补缩通道")
    return "；".join(hints) if hints else "结合该描述复查对应工序，数据不足时勿扩大结论"


def _avg(nums: list[float]) -> float | None:
    if not nums:
        return None
    return round(sum(nums) / len(nums), 1)


def _is_internal_yield_note(text: str) -> bool:
    """开发口径说明，不展示给业务查询页 / 导出文档。"""
    return "合格口径" in text or "产线维" in text


def _build_query_insights(
    *,
    inventory_guid: str,
    inventory: dict[str, Any] | None,
    stock_summary: dict[str, Any] | None,
    lines: list[dict[str, Any]],
    best_line: dict[str, Any] | None,
    combo_yield: dict[str, Any] | None,
    process_details: dict[str, list[dict[str, Any]]] | None,
    weather_yield: dict[str, Any] | None,
    warnings: list[str],
    order_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """查询页与导出文档共用的四段解读（不改写表格数字）。"""
    inv = inventory or {}
    order = _normalize_order_context(order_context)
    combo = (combo_yield or {}).get("bestCombo") if isinstance(combo_yield, dict) else None
    sample_ok = bool((combo_yield or {}).get("bestComboSampleOk")) if combo_yield else False
    code = str(inv.get("code") or "").strip() or "—"
    name = str(inv.get("name") or "").strip() or "—"

    summary: list[str] = []
    if order and order.get("saleOrderCode"):
        summary.append(
            f"本次查询订单 {order.get('saleOrderCode')} 本行物料 {code} {name}；"
            f"合同 {_fmt_num(order.get('billOrderQty'))}、计划 {_fmt_num(order.get('scheduOrderQty'))}、"
            f"本单在制 {_fmt_num(order.get('workingQty'))}、本单库存 {_fmt_num(order.get('stockQty'))}。"
            "下列良率与工序为该 InventoryGUID 的同型号历史，不是仅本订单。"
        )
    else:
        summary.append(f"分析物料 {code} {name}（InventoryGUID={inventory_guid}）。")

    kinds: dict[str, float] = {}
    for wh in list((stock_summary or {}).get("allWarehouses") or []):
        kind = _warehouse_kind(wh)
        kinds[kind] = kinds.get(kind, 0) + float(wh.get("availableQty") or 0)
    if stock_summary:
        bits = [f"{k} {_fmt_num(v)}" for k, v in kinds.items() if v]
        summary.append(
            f"全仓可用合计 {_fmt_num(stock_summary.get('allAvailableQty'))}"
            + (f"（{ '，'.join(bits) }）" if bits else "")
            + "。"
        )

    if combo:
        rate = combo.get("yieldRate")
        rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
        extra = "样本量够，可作为推荐。" if sample_ok else "投入不足 3，仅供观察、不得当绝对最优。"
        summary.append(
            f"推荐组合：班别 {combo.get('shiftName') or '—'} / 电炉 {combo.get('furnaceName') or '—'} / "
            f"月炉次 {combo.get('monthFurnaceNo') or '—'} / 箱号 {combo.get('boxNo') or '—'}，"
            f"良率 {rate_s}，投入 {_fmt_num(combo.get('inputQty'))}。{extra}"
        )
    elif best_line:
        rate = best_line.get("yieldRate")
        rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
        summary.append(
            f"暂无浇铸→二检组合，班组二检对照最优为 {best_line.get('lineName') or '—'}（{rate_s}）。"
        )
    else:
        summary.append("未汇总到二检良率，无法给出推荐组合。")

    arrange: list[str] = []
    if combo:
        arrange.append(
            f"优先按推荐组合排产：{combo.get('shiftName') or '—'}班、电炉 {combo.get('furnaceName') or '—'}、"
            f"参照月炉次 {combo.get('monthFurnaceNo') or '—'}、箱号 {combo.get('boxNo') or '—'}。"
        )
        casting_rows = list((process_details or {}).get("casting") or [])
        matched = [
            r
            for r in casting_rows
            if (not combo.get("furnaceName") or r.get("furnaceName") == combo.get("furnaceName"))
            and (
                not combo.get("monthFurnaceNo")
                or r.get("monthFurnaceNo") == combo.get("monthFurnaceNo")
            )
        ]
        src = matched or casting_rows
        ft = _avg(
            [float(r["furnaceTmpAvg"]) for r in src if isinstance(r.get("furnaceTmpAvg"), (int, float))]
        )
        ct = _avg(
            [float(r["castingTmpAvg"]) for r in src if isinstance(r.get("castingTmpAvg"), (int, float))]
        )
        if ft is not None or ct is not None:
            arrange.append(
                f"该组合相关浇铸记录炉温均值 {_fmt_num(ft)}℃、浇铸温均值 {_fmt_num(ct)}℃，排产时对照工艺卡，勿直接当标准。"
            )
        riser = inv.get("riser") if isinstance(inv.get("riser"), dict) else {}
        if riser.get("spec") or riser.get("name"):
            arrange.append(
                f"冒口规格 {riser.get('spec') or '—'}（{riser.get('name') or '—'}），材质 "
                f"{inv.get('materialTypeName') or inv.get('castingTypeKey') or '数据不足'}。"
            )
        if kinds.get("半成品") or kinds.get("废品"):
            arrange.append("先确认半成品/废品仓能否转成品或需报废处理，再决定是否新开浇铸。")
        if not sample_ok:
            arrange.append("因样本量不足，建议小批量验证后再扩大排产。")
    else:
        arrange.append("数据不足，暂无法给出可执行排产组合。")

    defects: list[str] = []
    seen: set[str] = set()
    for stage in ("second_inspect", "scrap", "first_inspect", "cut_inspect"):
        for row in list((process_details or {}).get(stage) or []):
            desc = str(row.get("sampleCauseDesc") or "").strip()
            if not desc or desc in seen:
                continue
            seen.add(desc)
            scrap = float(row.get("scrapQty") or 0)
            tag = "报废件" if scrap > 0 else "检验备注（判定仍可能合格）"
            defects.append(f"{tag}：{desc}。建议：{_defect_prevention(desc)}。")
            if len(defects) >= 6:
                break
        if len(defects) >= 6:
            break
    if not defects:
        defects.append("本次明细未抽到质量原因描述，缺陷与预防写「数据不足」，勿编造。")

    risks: list[str] = []
    if combo and not sample_ok:
        risks.append("推荐组合投入不足 3，统计上不能当作绝对最优。")
    if not inv.get("position"):
        risks.append("物料档案部位为空，文档与排产勿臆造部位名称。")
    wy = weather_yield if isinstance(weather_yield, dict) else {}
    for key, label in (("castingDay", "浇铸日"), ("inspectDay", "二检日")):
        block = wy.get(key) if isinstance(wy.get(key), dict) else None
        days = int((block or {}).get("sampleDays") or 0)
        if days and days < 5:
            risks.append(f"{label}气温分箱仅 {days} 天有效样本，不得写成气温因果。")
    for w in warnings or []:
        text = str(w).strip()
        if not text or text in risks or _is_internal_yield_note(text):
            continue
        risks.append(text)
    if not risks:
        risks.append("请以本页表格为准复核在制件号、库存与订单状态是否一致。")

    def _md(title: str, items: list[str]) -> str:
        body = "\n".join(f"- {x}" for x in items)
        return f"## {title}\n\n{body}"

    markdown = "\n\n".join(
        [
            _md("1. 结论摘要", summary),
            _md("8. 推荐生产安排", arrange),
            _md("9. 主要缺陷与预防", defects),
            _md("10. 风险与待确认项", risks),
        ]
    )
    return {
        "summary": summary,
        "arrange": arrange,
        "defects": defects,
        "risks": risks,
        "markdown": markdown,
    }


def _compose_yield_markdown(
    *,
    inventory: dict[str, Any] | None,
    inventory_guid: str,
    factual: str,
    narrative: str,
) -> str:
    inv = inventory or {}
    code = str(inv.get("code") or "").strip()
    name = str(inv.get("name") or "").strip()
    title = f"# 同型号最优良率实践 — {code} {name}".strip()
    note = (
        "> 物料档案、库存、良率表、工序明细、气温分箱均直接来自本次查询分析。"
        "结论摘要 / 推荐安排 / 缺陷预防 / 风险与查询页解读一致。"
    )
    parts = [title, note]
    narr = (narrative or "").strip()
    if narr:
        parts.append(narr)
    else:
        parts.append("## 1. 结论摘要\n\n（模型未返回结论，以下为查询分析原文。）")
    if factual:
        parts.append(factual)
    return "\n\n".join(parts).strip() + "\n"


def _build_raw_context(
    *,
    inventory_guid: str,
    inventory: dict[str, Any] | None,
    lines: list[dict[str, Any]],
    best_line: dict[str, Any] | None,
    warnings: list[str],
    weather_summary: dict[str, Any] | None = None,
    process_summary: dict[str, Any] | None = None,
    stock_summary: dict[str, Any] | None = None,
    weather_yield: dict[str, Any] | None = None,
    combo_yield: dict[str, Any] | None = None,
    order_context: dict[str, Any] | None = None,
) -> str:
    inv = inventory or {}
    riser = inv.get("riser") if isinstance(inv.get("riser"), dict) else {}
    parts: list[str] = []
    order = _normalize_order_context(order_context)
    if order:
        parts.extend(_order_context_lines(order))
        parts.append("")
    parts.extend(
        [
            "## 物料",
            f"- InventoryGUID: {inventory_guid}",
            f"- 物料编码: {inv.get('code')}",
            f"- 物料名称: {inv.get('name')}",
            f"- 规格: {inv.get('spec')}",
            f"- 产品尺寸(A×B×H): {inv.get('productSize')}",
        ]
    )
    if inv.get("position"):
        parts.append(f"- 部位: {inv.get('position')}")
    parts.extend(
        [
            f"- 形状: {inv.get('shape') or '数据不足'}",
            f"- 单重: {_fmt_num(inv.get('weightGross'))}",
            f"- 体积: {_fmt_num(inv.get('volume'))}",
            f"- 材质: {inv.get('materialTypeName') or '数据不足'}"
            + (
                f"（GUID {inv.get('materialTypeGuid')}）"
                if inv.get("materialTypeGuid") and not inv.get("materialTypeName")
                else ""
            ),
            f"- 浇筑类型: {inv.get('castingTypeName') or inv.get('castingTypeKey') or '数据不足'}",
            f"- 产品类型: {inv.get('productTypeName') or inv.get('productTypeCode') or '数据不足'}",
            f"- 冒口: {riser.get('name') or '数据不足'} / 规格 {riser.get('spec') or '数据不足'} / 编码 {riser.get('code') or '—'}",
            f"- 割冒口面积: {_fmt_num(inv.get('cutRiserArea'))}；机加工面积: {_fmt_num(inv.get('machiningArea'))}；钻孔深: {_fmt_num(inv.get('drillingDepth'))}",
            f"- 档案退火天数: {_fmt_num(inv.get('productAnnealingDays'))}",
            "",
            "## 库存（分析前）",
            "来源：invn.Stock 全仓结存；类型「成品」为成品相关仓标记",
            "",
        ]
    )
    if stock_summary:
        parts.append(
            f"- 全仓可用合计: {_fmt_num(stock_summary.get('allAvailableQty'))}；"
            f"占用: {_fmt_num(stock_summary.get('allLockedQty'))}；"
            f"在途/预留: {_fmt_num(stock_summary.get('allInTransitQty'))}；"
            f"合计: {_fmt_num(stock_summary.get('allTotalQty'))}"
        )
        if stock_summary.get("availableQty") is not None:
            parts.append(
                f"- 其中成品仓可用: {_fmt_num(stock_summary.get('availableQty'))}"
            )

        all_whs = list(stock_summary.get("allWarehouses") or [])
        if all_whs:
            parts.append("")
            parts.append("### 全仓明细")
            parts.append("| 类型 | 仓库 | 编码 | 可用 | 占用 | 在途 | 小计 |")
            parts.append("| --- | --- | --- | --- | --- | --- | --- |")
            for w in all_whs[:30]:
                parts.append(
                    f"| {w.get('category') or '—'} | {w.get('warehouseName') or '—'} | "
                    f"{w.get('warehouseCode') or '—'} | {_fmt_num(w.get('availableQty'))} | "
                    f"{_fmt_num(w.get('lockedQty'))} | {_fmt_num(w.get('inTransitQty'))} | "
                    f"{_fmt_num(w.get('totalQty'))} |"
                )
        else:
            parts.append("- 无全仓结存行")
    else:
        parts.append("- （未查询到库存摘要）")

    combo = combo_yield or {}
    combos = list(combo.get("combos") or [])
    parts.extend(
        [
            "",
            "## 组合良率（新口径，优先）",
            "链路：浇铸任务在制件 → 该件最终二检。维度：班别 × 电炉 × 月炉次 × 箱号。",
            "砂型未纳入组合键。气温分箱为观察性。投入不足 3 不得当绝对最优。",
            "",
        ]
    )
    best_combo = combo.get("bestCombo")
    if best_combo:
        rate = best_combo.get("yieldRate")
        rate_s = f"{rate:.2%}" if isinstance(rate, float) else "—"
        parts.append(
            f"**推荐组合**：班别 {best_combo.get('shiftName') or '—'} / "
            f"电炉 {best_combo.get('furnaceName') or '—'} / "
            f"月炉次 {best_combo.get('monthFurnaceNo') or '—'} / "
            f"箱号 {best_combo.get('boxNo') or '—'}；"
            f"良率 {rate_s}；投入 {_fmt_num(best_combo.get('inputQty'))}"
        )
        parts.append("")
    if not combos:
        parts.append("（无浇铸→二检组合行）")
    else:
        parts.append("| 班别 | 电炉 | 月炉次 | 箱号 | 批次 | 投入 | 合格 | 报废 | 良率 | 日期区间 |")
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in combos[:16]:
            rate = row.get("yieldRate")
            rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
            parts.append(
                f"| {row.get('shiftName') or '—'} | {row.get('furnaceName') or '—'} | "
                f"{row.get('monthFurnaceNo') or '—'} | {row.get('boxNo') or '—'} | "
                f"{_fmt_num(row.get('lotCount'))} | {_fmt_num(row.get('inputQty'))} | "
                f"{_fmt_num(row.get('passQty'))} | {_fmt_num(row.get('scrapQty'))} | "
                f"{rate_s} | {row.get('dateFrom') or '—'}~{row.get('dateTo') or '—'} |"
            )
        rest = int(combo.get("combosTruncated") or 0)
        if rest > 0:
            parts.append(f"- …另有 {rest} 组省略")
    weather_combos = list(combo.get("weatherCombos") or [])
    if weather_combos:
        parts.append("")
        parts.append("### 组合 × 气温分箱（观察）")
        parts.append("| 班别 | 电炉 | 月炉次 | 箱号 | 气温 | 投入 | 合格 | 良率 |")
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in weather_combos[:12]:
            rate = row.get("yieldRate")
            rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
            parts.append(
                f"| {row.get('shiftName') or '—'} | {row.get('furnaceName') or '—'} | "
                f"{row.get('monthFurnaceNo') or '—'} | {row.get('boxNo') or '—'} | "
                f"{row.get('weatherBin') or '—'} | {_fmt_num(row.get('inputQty'))} | "
                f"{_fmt_num(row.get('passQty'))} | {rate_s} |"
            )

    parts.extend(
        [
            "",
            "## 产线良率排名（原口径，班组二检）",
            "（口径已确认：二检；空或1x=合格、2x=报废；良率=合格/投入；产线=班组）",
            "",
        ]
    )
    if not lines:
        parts.append("（无结构化良率行）")
    else:
        parts.append("| 排名 | 班组 | 编码 | 良率 | 批次 | 投入 | 合格 | 报废 |")
        parts.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, line in enumerate(lines, start=1):
            rate = line.get("yieldRate")
            rate_s = f"{rate:.2%}" if isinstance(rate, float) else str(rate)
            parts.append(
                f"| {i} | {line.get('lineName')} | {line.get('lineCode')} | "
                f"{rate_s} | {line.get('lotCount')} | {line.get('inputQty')} | "
                f"{line.get('passQty')} | {line.get('scrapQty')} |"
            )
    if best_line:
        parts.append("")
        parts.append(
            f"**班组口径最优（对照，非组合）**：{best_line.get('lineName')} / {best_line.get('lineCode')}"
        )

    # 工序工艺 × 室外气温对照（按砂型 / 熔铸 / 加工三大类）
    proc_stages = (process_summary or {}).get("stages") or {}
    if proc_stages:
        parts.append("")
        parts.append("## 工序工艺对照（含室外气温）")
        parts.append(
            "按 InventoryGUID 过滤，分砂型作业 / 熔铸作业 / 加工作业。"
            "「室外均温」为厂区观测值；浇铸「炉温/浇铸温」为 MES 工艺温度。"
            "切冒计划主要对应浇筑类型 WS，PT 物料常无记录。"
            "组装作业本阶段未纳入。"
        )
        inv_code = inventory.get("code") if inventory else None
        inv_name = inventory.get("name") if inventory else None
        parts.append(f"- 本段物料：{inv_code} / {inv_name}")
        for grp in PROCESS_GROUPS:
            parts.append("")
            parts.append(f"## {grp['label']}")
            for key in grp["stages"]:
                block = proc_stages.get(key)
                if not isinstance(block, dict):
                    continue
                rows = list(block.get("rows") or [])
                label = block.get("label") or STAGE_WEATHER_LABELS.get(key, key)
                parts.append("")
                parts.append(f"### {label}")
                if not rows:
                    parts.append("（无工艺行）")
                    continue
                _append_process_markdown(parts, key, rows)

    if weather_summary:
        parts.append("")
        parts.append("## 历史气温摘要（观察性）")
        parts.append(
            "按三大类工序日期关联：砂型作业（班计划/打型/粘型）、"
            "熔铸作业（电炉/浇铸计划/组型/浇铸/出箱/一检）、"
            "加工作业（切冒/改型/切检/加工日计划/加工任务/二检/实物报废/终检）"
        )
        loc = weather_summary.get("location") or {}
        if loc:
            parts.append(
                f"- 位置: {loc.get('name')} ({loc.get('latitude')}, {loc.get('longitude')})"
            )
        stages = weather_summary.get("stages") or {}
        if stages:
            for key in STAGE_PROCESS_ORDER:
                block = stages.get(key)
                if not block:
                    continue
                parts.append(
                    f"- {block.get('label')}: {block.get('dayCount')}天；"
                    f"均温均值 {block.get('tempMeanAvgC')}℃；"
                    f"区间 {block.get('tempMeanMinC')}~{block.get('tempMeanMaxC')}℃"
                )
        else:
            summary = weather_summary.get("summary") or {}
            parts.append(
                f"- 样本天数: {summary.get('dayCount')}；"
                f"均温区间: {summary.get('tempMeanMinC')}~{summary.get('tempMeanMaxC')}℃；"
                f"均温均值: {summary.get('tempMeanAvgC')}℃"
            )

    if weather_yield:
        parts.append("")
        parts.append("## 气温与良率对照（分箱，观察性）")
        parts.append("样本量不足时不得宣称气温因果；只陈述区间差异。")
        for wy_key in ("castingDay", "inspectDay"):
            block = weather_yield.get(wy_key) if isinstance(weather_yield, dict) else None
            if not isinstance(block, dict):
                continue
            parts.append("")
            parts.append(f"### {block.get('basis')}")
            parts.append(f"- {block.get('note')}")
            parts.append(f"- 有效天数: {block.get('sampleDays')}")
            bins = list(block.get("bins") or [])
            if bins:
                parts.append("| 气温区间 | 天数 | 投入 | 合格 | 报废 | 良率 |")
                parts.append("| --- | --- | --- | --- | --- | --- |")
                for b in bins:
                    rate = b.get("yieldRate")
                    rate_s = f"{rate:.1%}" if isinstance(rate, float) else "—"
                    parts.append(
                        f"| {b.get('label')} | {b.get('dayCount')} | "
                        f"{_fmt_num(b.get('inputQty'))} | {_fmt_num(b.get('passQty'))} | "
                        f"{_fmt_num(b.get('scrapQty'))} | {rate_s} |"
                    )
            best_bin = block.get("bestBin") or {}
            worst_bin = block.get("worstBin") or {}
            if best_bin:
                parts.append(
                    f"- 样本内较高良率区间: {best_bin.get('label')}（{best_bin.get('yieldRate')}）"
                )
            if worst_bin:
                parts.append(
                    f"- 样本内较低良率区间: {worst_bin.get('label')}（{worst_bin.get('yieldRate')}）"
                )

    public_warnings = [w for w in warnings if str(w).strip() and not _is_internal_yield_note(str(w))]
    if public_warnings:
        parts.append("")
        parts.append("## 警告")
        parts.extend(f"- {w}" for w in public_warnings)
    parts.append("")
    parts.append(
        "请仅依据以上数据生成《同型号最优良率实践》Markdown，禁止编造数字。"
        "物料须写部位、尺寸、冒口规格；材质/浇筑类型若只有 GUID 则写数据不足，不得编名称。"
        "结论优先引用「组合良率」推荐组合（班别×电炉×月炉次×箱号）；"
        "班组二检排名仅作对照。"
        "须按砂型作业 / 熔铸作业 / 加工作业三大类写工序明细"
        "（含电炉、组型、出箱、切冒、改型、加工日计划、实物报废、终检；无记录写数据不足），"
        "以及气温-良率分箱。"
        "若上文含「库存（分析前）」或全仓明细，文档中必须包含库存小节"
        "（成品仓可用、全仓可用及分仓），不得省略。"
    )
    return "\n".join(parts)


async def generate_yield_document(
    db: AsyncSession,
    *,
    inventory_guid: str | None = None,
    query: str | None = None,
    include_weather: bool = True,
    prompt_id: str | None = None,
    knowledge_base_id: str | None = None,
    export_to_filesystem: bool = False,
    mode: str = "deep",
    uploader: str | None = None,
    progress: ProgressCb = None,
    raw_context: str | None = None,
    inventory_snapshot: dict[str, Any] | None = None,
    order_context: dict[str, Any] | None = None,
    insights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用本次分析表格拼文档；结论与查询页 insights 一致。"""
    order = _normalize_order_context(order_context)
    reused = bool((raw_context or "").strip() and (inventory_guid or "").strip())
    logger.warning(
        "casting.yield document start reused=%s guid=%s ctx_len=%s prompt=%s has_order=%s",
        reused,
        (inventory_guid or "").strip(),
        len((raw_context or "").strip()),
        prompt_id,
        bool(order),
    )
    if reused:
        await _emit_progress(
            progress, step="document", label="使用已有分析结果生成文档", status="running"
        )
        inv_snap = inventory_snapshot if isinstance(inventory_snapshot, dict) else {}
        ctx = _ensure_order_in_raw_context(str(raw_context or ""), order)
        analysis: dict[str, Any] = {
            "found": True,
            "message": "ok",
            "inventoryGuid": (inventory_guid or "").strip(),
            "inventory": inv_snap,
            "rawContext": ctx,
            "orderContext": order,
            "warnings": [],
            "lines": [],
            "bestLine": None,
            "stockSummary": None,
            "processSummary": None,
            "weatherSummary": None,
            "weatherYield": None,
            "insights": insights if isinstance(insights, dict) else None,
        }
    else:
        analysis = await analyze_yield(
            db,
            inventory_guid=inventory_guid,
            query=query,
            include_weather=include_weather,
            progress=progress,
            order_context=order,
        )
    if not analysis.get("found"):
        return {
            **analysis,
            "markdown": "",
            "document": None,
            "fileExport": None,
        }

    await _emit_progress(
        progress, step="document", label="正在生成文档", status="running"
    )
    guid = str(analysis.get("inventoryGuid") or inventory_guid or "").strip()
    inv = analysis.get("inventory") if isinstance(analysis.get("inventory"), dict) else {}
    user_content = _ensure_order_in_raw_context(
        str(analysis.get("rawContext") or ""),
        order or _normalize_order_context(analysis.get("orderContext")),
    )
    analysis["rawContext"] = user_content
    factual = _factual_markdown_from_context(user_content)
    insight_md = ""
    if isinstance(insights, dict):
        insight_md = str(insights.get("markdown") or "").strip()
    if not insight_md and isinstance(analysis.get("insights"), dict):
        insight_md = str((analysis.get("insights") or {}).get("markdown") or "").strip()

    if insight_md:
        narrative = insight_md
        logger.warning("casting.yield document using query insights narrative")
    else:
        from api.services.models.runtime import build_llm_client

        client = await build_llm_client(db, mode if mode in {"fast", "deep"} else "deep")
        client.timeout_seconds = max(float(client.timeout_seconds or 0), 180.0)
        client.fixed_temperature = 0.2
        _ = prompt_id
        try:
            llm_raw = await client.complete(
                [
                    {"role": "system", "content": _NARRATIVE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            factual
                            + "\n\n请只输出 ## 1. 结论摘要 / ## 8. 推荐生产安排 / "
                            "## 9. 主要缺陷与预防 / ## 10. 风险与待确认项。"
                            "不要复制或改写上文任何表格。"
                        ),
                    },
                ],
                mode=mode if mode in {"fast", "deep"} else "deep",
            )
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("casting.yield llm complete failed")
            detail = (str(exc) or "").strip() or type(exc).__name__
            raise AppError(
                ErrorCode.INTERNAL,
                f"模型生成失败（{type(exc).__name__}，超时 {client.timeout_seconds:.0f}s）：{detail}",
                status_code=502,
            ) from exc
        narrative = _extract_narrative_sections(llm_raw or "")
        if not narrative and (llm_raw or "").strip() and "| --- |" not in (llm_raw or ""):
            narrative = (llm_raw or "").strip()
    markdown = _compose_yield_markdown(
        inventory=inv,
        inventory_guid=guid,
        factual=factual,
        narrative=narrative,
    )
    if not markdown.strip():
        raise AppError(
            ErrorCode.INTERNAL,
            "未能组装文档。请检查分析结果后重试。",
            status_code=502,
        )

    document = None
    kb_id = (knowledge_base_id or "").strip()
    guid = str(analysis.get("inventoryGuid") or inventory_guid or "").strip()
    inv = analysis.get("inventory") or {}
    if kb_id and markdown:
        from api.services.knowledge.ingest import ingest_text

        code = inv.get("code") or (guid[:8] if guid else "unknown")
        line = (analysis.get("bestLine") or {}).get("lineCode") or ""
        title = f"[最优产线] {code} / {line} / {guid}"
        document = await ingest_text(
            db,
            base_public_id=kb_id,
            name=title[:200],
            content=markdown,
            source="text",
            uploader=uploader,
            tags=["casting-yield", str(code), guid],
        )

    file_export = None
    warnings = list(analysis.get("warnings") or [])
    if export_to_filesystem and markdown:
        try:
            file_export = await export_markdown_via_filesystem(
                db,
                markdown=markdown,
                inventory_guid=guid,
                inventory_name=str(inv.get("name") or ""),
                inventory_code=str(inv.get("code") or ""),
            )
        except AppError as exc:
            logger.warning("casting.yield export_filesystem failed: %s", exc)
            file_export = {"ok": False, "error": str(getattr(exc, "msg", None) or exc)}
            warnings.append(f"本地 Markdown 导出失败：{file_export.get('error')}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("casting.yield export_filesystem failed")
            file_export = {"ok": False, "error": str(exc)}
            warnings.append(f"本地 Markdown 导出失败：{exc}")

    await _emit_progress(
        progress, step="document", label="正在生成文档", status="done"
    )
    return {
        **analysis,
        "warnings": warnings,
        "markdown": markdown,
        "document": document,
        "fileExport": file_export,
    }
