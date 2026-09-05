"""合同 PT 砖材脱棱角（213）统计。

口径（已确认）：
- 脱角只计一检 BadDictionaryCodeList 含 213（脱棱角 / 自然脱棱角）；212 缺棱角不计
- 出砖 = 合同物料（InventoryGUID）的全部浇铸记录，不限本单在制件
- 合同：SaleContractCode → SaleOrder.SaleContractGUID
- PT：材质名 LIKE %PT%（现库为 33#PT）
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.casting.yield_analysis import (
    _parse_mcp_rows,
    _row_get,
    _sql_escape,
    find_mssql_server,
)
from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PEEL_CODE = "213"

# 报表「待口」对应一检描述里的铸口/待口/带口
_POS_MOUTH = "待口"
_POS_BOTTOM = "底部"
_POS_FACE = "面"
_POS_OTHER = "其他"


def _require_date(value: str | None, *, field: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not _DATE_RE.match(text):
        raise AppError(ErrorCode.VALIDATION, f"{field} 须为 YYYY-MM-DD", status_code=422)
    return text


def code_list_is_peel_213(code_list: str | None) -> bool:
    """一检不良码是否含 213（不含 212）。"""
    padded = "," + str(code_list or "").replace(" ", "") + ","
    return ",213," in padded or ",213:" in padded


def classify_peel_position(desc: str | None) -> str:
    """从一检原因描述拆脱角部位。铸口面优先归待口，避免被「面」抢走。"""
    text = str(desc or "").strip()
    if any(key in text for key in ("铸口", "待口", "带口")):
        return _POS_MOUTH
    if "底部" in text or "底" in text:
        return _POS_BOTTOM
    if "面" in text:
        return _POS_FACE
    return _POS_OTHER


def _peel_sql_predicate(alias: str = "q") -> str:
    """T-SQL：BadDictionaryCodeList 含 213 或 213:…"""
    expr = f"N',' + REPLACE(ISNULL({alias}.BadDictionaryCodeList, N''), N' ', N'') + N','"
    return (
        f"({expr} LIKE N'%,{_PEEL_CODE},%' OR {expr} LIKE N'%,{_PEEL_CODE}:%')"
    )


def _date_sql(date_from: str | None, date_to: str | None) -> str:
    parts: list[str] = []
    if date_from:
        parts.append(f"AND b.CastedDate >= '{date_from}'")
    if date_to:
        parts.append(f"AND b.CastedDate < DATEADD(day, 1, '{date_to}')")
    return (" " + " ".join(parts)) if parts else ""


def sql_contract_pt_specs(contract_code: str, material_like: str) -> str:
    code = _sql_escape(contract_code)
    mat = _sql_escape(material_like)
    return f"""
SELECT TOP 200
  CAST(si.InventoryGUID AS nvarchar(36)) AS InventoryGUID,
  MAX(mt.ProductMateralTypeName) AS MaterialName,
  MAX(inv.InventoryCode) AS InventoryCode,
  MAX(inv.InventoryName) AS InventoryName,
  MAX(inv.InventorySpecification) AS SpecName
FROM sale.SaleContract c
INNER JOIN sale.SaleOrder o ON o.SaleContractGUID = c.SaleContractGUID
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
INNER JOIN invn.Inventory inv ON inv.InventoryGUID = si.InventoryGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'{code}'
  AND mt.ProductMateralTypeName LIKE N'%{mat}%'
GROUP BY si.InventoryGUID
ORDER BY MAX(inv.InventoryCode)
""".strip()


def sql_peel_by_day(
    guid_in: str,
    *,
    date_from: str | None,
    date_to: str | None,
) -> str:
    peel = _peel_sql_predicate("q")
    date_sql = _date_sql(date_from, date_to)
    return f"""
SELECT TOP 400
  CONVERT(varchar(10), b.CastedDate, 23) AS CastDate,
  ISNULL(ws.WorkShiftCode, N'UNKNOWN') AS ShiftCode,
  ISNULL(ws.WorkShiftName, N'未识别班别') AS ShiftName,
  ISNULL(furn.FurnaceName, N'未识别电炉') AS FurnaceName,
  COUNT(DISTINCT mo.MakingObjectGUID) AS BrickCnt,
  COUNT(DISTINCT CASE
    WHEN peel.MakingObjectGUID IS NOT NULL THEN mo.MakingObjectGUID
  END) AS PeelCnt
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
LEFT JOIN make.WorkShift ws
  ON ws.WorkShiftGUID = COALESCE(b.CastWorkShiftGUID, b.WorkShiftGUID)
LEFT JOIN (
  SELECT DISTINCT q.MakingObjectGUID
  FROM invn.QualityFirstStage q
  WHERE {peel}
) peel ON peel.MakingObjectGUID = mo.MakingObjectGUID
OUTER APPLY (
  SELECT TOP 1 p.FurnaceName AS FurnaceName
  FROM make.WorkCastingFurnaceItem fi
  INNER JOIN make.WorkCastingFurnace f
    ON f.WorkCastingFurnaceGUID = fi.WorkCastingFurnaceGUID
  LEFT JOIN make.WorkCastingPlan p
    ON p.WorkCastingPlanGUID = f.WorkCastingPlanGUID
  WHERE fi.MakingObjectGUID = bi.MakingObjectGUID
  ORDER BY COALESCE(fi.CastingTime, f.BusinessDate) DESC
) furn
WHERE mo.InventoryGUID IN ({guid_in})
  AND b.CastedDate IS NOT NULL
  {date_sql}
GROUP BY
  CONVERT(varchar(10), b.CastedDate, 23),
  ws.WorkShiftCode,
  ws.WorkShiftName,
  furn.FurnaceName
ORDER BY
  CONVERT(varchar(10), b.CastedDate, 23),
  furn.FurnaceName,
  ws.WorkShiftName
""".strip()


def sql_peel_positions(
    guid_in: str,
    *,
    date_from: str | None,
    date_to: str | None,
) -> str:
    peel = _peel_sql_predicate("q")
    date_sql = _date_sql(date_from, date_to)
    return f"""
SELECT TOP 200
  ISNULL(q.QualityCauseDesc, N'') AS CauseDesc,
  COUNT(DISTINCT q.MakingObjectGUID) AS PeelCnt
FROM invn.QualityFirstStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
INNER JOIN make.WorkCastingInBoxItem bi
  ON bi.MakingObjectGUID = mo.MakingObjectGUID
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
WHERE mo.InventoryGUID IN ({guid_in})
  AND b.CastedDate IS NOT NULL
  AND {peel}
  {date_sql}
GROUP BY ISNULL(q.QualityCauseDesc, N'')
ORDER BY COUNT(DISTINCT q.MakingObjectGUID) DESC
""".strip()


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _rate(peel: int, brick: int) -> float | None:
    if brick <= 0:
        return None
    return round(peel / brick * 1000) / 10.0


def _shift_sort_key(name: str) -> tuple[int, str]:
    order = {"甲": 0, "乙": 1, "丙": 2, "丁": 3}
    if name in order:
        return (order[name], name)
    if "未识别" in name:
        return (90, name)
    return (50, name)


def _furnace_sort_key(name: str) -> tuple[int, str]:
    if "未识别" in name:
        return (90, name)
    prefer = {"南": 0, "北": 1, "A": 2, "B": 3}
    for key, idx in prefer.items():
        if key in name:
            return (idx, name)
    return (40, name)


def _metrics(brick: int, peel: int) -> dict[str, Any]:
    return {
        "brickCnt": brick,
        "peelCnt": peel,
        "peelRate": _rate(peel, brick),
    }


def build_pivot(daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把「日×电炉×班别」汇总成横向日期透视。"""
    cells: dict[tuple[str, str, str], dict[str, int]] = {}
    dates: set[str] = set()
    furnaces: set[str] = set()
    shifts: set[str] = set()

    for row in daily_rows:
        date_s = str(_row_get(row, "CastDate", "castDate") or "")[:10]
        if len(date_s) < 10:
            continue
        furnace = str(_row_get(row, "FurnaceName", "furnaceName") or "未识别电炉").strip() or "未识别电炉"
        shift = str(_row_get(row, "ShiftName", "shiftName") or "未识别班别").strip() or "未识别班别"
        brick = _as_int(_row_get(row, "BrickCnt", "brickCnt"))
        peel = _as_int(_row_get(row, "PeelCnt", "peelCnt"))
        key = (furnace, shift, date_s)
        cur = cells.get(key) or {"brick": 0, "peel": 0}
        cur["brick"] += brick
        cur["peel"] += peel
        cells[key] = cur
        dates.add(date_s)
        furnaces.add(furnace)
        shifts.add(shift)

    date_list = sorted(dates)
    furnace_list = sorted(furnaces, key=_furnace_sort_key)
    shift_list = sorted(shifts, key=_shift_sort_key)

    def cell(furnace: str, shift: str, date_s: str) -> dict[str, int]:
        return cells.get((furnace, shift, date_s)) or {"brick": 0, "peel": 0}

    table_rows: list[dict[str, Any]] = []
    grand_brick = 0
    grand_peel = 0
    furnace_totals: dict[str, dict[str, int]] = {}
    shift_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"brick": 0, "peel": 0})
    daily_tot: dict[str, dict[str, int]] = {d: {"brick": 0, "peel": 0} for d in date_list}
    daily_shift: dict[str, dict[str, dict[str, int]]] = {
        d: {s: {"brick": 0, "peel": 0} for s in shift_list} for d in date_list
    }

    for furnace in furnace_list:
        f_brick = 0
        f_peel = 0
        f_by_date = {d: {"brick": 0, "peel": 0} for d in date_list}
        for shift in shift_list:
            s_brick = 0
            s_peel = 0
            by_date: dict[str, dict[str, Any]] = {}
            for date_s in date_list:
                c = cell(furnace, shift, date_s)
                s_brick += c["brick"]
                s_peel += c["peel"]
                f_by_date[date_s]["brick"] += c["brick"]
                f_by_date[date_s]["peel"] += c["peel"]
                daily_tot[date_s]["brick"] += c["brick"]
                daily_tot[date_s]["peel"] += c["peel"]
                daily_shift[date_s][shift]["brick"] += c["brick"]
                daily_shift[date_s][shift]["peel"] += c["peel"]
                by_date[date_s] = _metrics(c["brick"], c["peel"])
            f_brick += s_brick
            f_peel += s_peel
            shift_totals[shift]["brick"] += s_brick
            shift_totals[shift]["peel"] += s_peel
            table_rows.append(
                {
                    "kind": "shift",
                    "furnaceName": furnace,
                    "shiftName": shift,
                    "total": _metrics(s_brick, s_peel),
                    "byDate": by_date,
                }
            )
        furnace_totals[furnace] = {"brick": f_brick, "peel": f_peel}
        grand_brick += f_brick
        grand_peel += f_peel
        table_rows.append(
            {
                "kind": "furnaceTotal",
                "furnaceName": furnace,
                "shiftName": "合计",
                "total": _metrics(f_brick, f_peel),
                "byDate": {
                    d: _metrics(f_by_date[d]["brick"], f_by_date[d]["peel"]) for d in date_list
                },
            }
        )

    table_rows.append(
        {
            "kind": "grandTotal",
            "furnaceName": "总计",
            "shiftName": "",
            "total": _metrics(grand_brick, grand_peel),
            "byDate": {
                d: _metrics(daily_tot[d]["brick"], daily_tot[d]["peel"]) for d in date_list
            },
        }
    )
    for shift in shift_list:
        tot = shift_totals[shift]
        table_rows.append(
            {
                "kind": "shiftTotal",
                "furnaceName": "",
                "shiftName": shift,
                "total": _metrics(tot["brick"], tot["peel"]),
                "byDate": {
                    d: _metrics(
                        daily_shift[d][shift]["brick"],
                        daily_shift[d][shift]["peel"],
                    )
                    for d in date_list
                },
            }
        )

    furnace_pie = [
        {
            "name": name,
            **_metrics(tot["brick"], tot["peel"]),
        }
        for name, tot in sorted(furnace_totals.items(), key=lambda kv: _furnace_sort_key(kv[0]))
    ]
    shift_pie = [
        {
            "name": name,
            **_metrics(tot["brick"], tot["peel"]),
        }
        for name, tot in sorted(shift_totals.items(), key=lambda kv: _shift_sort_key(kv[0]))
    ]
    daily_bars = []
    for date_s in date_list:
        item: dict[str, Any] = {
            "name": date_s[5:],
            "date": date_s,
            "平均": _rate(daily_tot[date_s]["peel"], daily_tot[date_s]["brick"]) or 0,
        }
        for shift in shift_list:
            item[shift] = _rate(
                daily_shift[date_s][shift]["peel"],
                daily_shift[date_s][shift]["brick"],
            ) or 0
        daily_bars.append(item)

    return {
        "dates": date_list,
        "furnaces": furnace_list,
        "shifts": shift_list,
        "rows": table_rows,
        "furnacePie": furnace_pie,
        "shiftPie": shift_pie,
        "dailyBars": daily_bars,
        "summary": _metrics(grand_brick, grand_peel),
    }


def build_position_pie(cause_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, int] = {
        _POS_MOUTH: 0,
        _POS_BOTTOM: 0,
        _POS_FACE: 0,
        _POS_OTHER: 0,
    }
    for row in cause_rows:
        pos = classify_peel_position(str(_row_get(row, "CauseDesc", "causeDesc") or ""))
        buckets[pos] = buckets.get(pos, 0) + _as_int(_row_get(row, "PeelCnt", "peelCnt"))
    total = sum(buckets.values())
    order = (_POS_MOUTH, _POS_BOTTOM, _POS_FACE, _POS_OTHER)
    out: list[dict[str, Any]] = []
    for name in order:
        cnt = buckets.get(name, 0)
        if cnt <= 0:
            continue
        share = round(cnt / total * 10000) / 100.0 if total else 0.0
        out.append({"name": name, "peelCnt": cnt, "share": share})
    return out


def _guid_in_list(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        guid = str(_row_get(row, "InventoryGUID", "inventoryGuid", "GUID") or "").strip()
        if not guid or guid in seen:
            continue
        seen.add(guid)
        out.append(guid.replace("'", ""))
    return out


async def query_peel_report(
    db: AsyncSession,
    *,
    contract_code: str,
    date_from: str | None = None,
    date_to: str | None = None,
    material_like: str = "PT",
) -> dict[str, Any]:
    code = (contract_code or "").strip()
    if not code:
        raise AppError(ErrorCode.VALIDATION, "contractCode required", status_code=422)
    mat = (material_like or "PT").strip() or "PT"
    d0 = _require_date(date_from, field="dateFrom")
    d1 = _require_date(date_to, field="dateTo")

    from api.services.mcp.isolated_stdio import (
        run_execute_queries_isolated,
        snapshot_mcp_server,
    )

    server = await find_mssql_server(db)
    snap = snapshot_mcp_server(server)
    rules = {
        "peelCode": _PEEL_CODE,
        "peelName": "脱棱角",
        "excludeCodes": ["212"],
        "brickScope": "contract_inventory_all_casted",
        "materialLike": mat,
    }

    spec_sql = sql_contract_pt_specs(code, mat)
    try:
        spec_batch = await asyncio.to_thread(
            run_execute_queries_isolated,
            server_snapshot=snap,
            steps=[("specs", spec_sql)],
            timeout_seconds=90.0,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("peel_report specs failed contract=%s", code)
        raise AppError(ErrorCode.INTERNAL, f"合同规格查询失败: {exc}", status_code=502) from exc

    spec_block = spec_batch.get("specs") or {}
    spec_rows = spec_block.get("rows")
    if not isinstance(spec_rows, list):
        spec_rows = _parse_mcp_rows(spec_block.get("content") or spec_block.get("raw"))
    guids = _guid_in_list(spec_rows)
    if not guids:
        return {
            "found": False,
            "message": f"合同 {code} 下没有材质含「{mat}」的订货物料",
            "contractCode": code,
            "dateFrom": d0,
            "dateTo": d1,
            "specCnt": 0,
            "specs": [],
            "rules": rules,
            "dates": [],
            "furnaces": [],
            "shifts": [],
            "rows": [],
            "furnacePie": [],
            "shiftPie": [],
            "dailyBars": [],
            "positionPie": [],
            "summary": _metrics(0, 0),
            "warnings": [],
        }

    guid_in = ", ".join(f"'{g}'" for g in guids)
    steps = [
        ("daily", sql_peel_by_day(guid_in, date_from=d0, date_to=d1)),
        ("position", sql_peel_positions(guid_in, date_from=d0, date_to=d1)),
    ]
    try:
        batch = await asyncio.to_thread(
            run_execute_queries_isolated,
            server_snapshot=snap,
            steps=steps,
            timeout_seconds=120.0,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("peel_report mes failed contract=%s", code)
        raise AppError(ErrorCode.INTERNAL, f"脱棱角统计查询失败: {exc}", status_code=502) from exc

    def _rows(step: str) -> list[dict[str, Any]]:
        block = batch.get(step) or {}
        rows = block.get("rows")
        if not isinstance(rows, list):
            rows = _parse_mcp_rows(block.get("content") or block.get("raw"))
        return rows

    daily_rows = _rows("daily")
    position_rows = _rows("position")
    pivot = build_pivot(daily_rows)
    warnings: list[str] = []
    if len(guids) >= 200:
        warnings.append("合同 PT 规格达到查询上限 200，结果可能被截断")
    if len(daily_rows) >= 400:
        warnings.append("日汇总行达到查询上限 400，日期或电炉班别可能被截断")

    specs = [
        {
            "inventoryGuid": str(_row_get(r, "InventoryGUID") or ""),
            "code": _row_get(r, "InventoryCode"),
            "name": _row_get(r, "InventoryName"),
            "spec": _row_get(r, "SpecName"),
            "materialName": _row_get(r, "MaterialName"),
        }
        for r in spec_rows
        if _row_get(r, "InventoryGUID")
    ]

    logger.warning(
        "peel_report ok contract=%s specs=%s bricks=%s peel=%s dates=%s",
        code,
        len(guids),
        (pivot.get("summary") or {}).get("brickCnt"),
        (pivot.get("summary") or {}).get("peelCnt"),
        len(pivot.get("dates") or []),
    )
    return {
        "found": True,
        "message": f"合同 {code} · {mat} · 213脱棱角",
        "contractCode": code,
        "dateFrom": d0 or (pivot["dates"][0] if pivot["dates"] else None),
        "dateTo": d1 or (pivot["dates"][-1] if pivot["dates"] else None),
        "specCnt": len(guids),
        "specs": specs,
        "rules": rules,
        **pivot,
        "positionPie": build_position_pie(position_rows),
        "warnings": warnings,
    }
