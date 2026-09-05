"""只读探测：脱角不良码 + 合同 25091 的 PT 规格数（口径对齐）。

用法：
  venv\\Scripts\\python.exe scripts\\probe_peel_defect_25091.py p2

经已配置的 MSSQL MCP execute_query 查 BestMES；失败的步骤会打印错误并继续。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "packages"), str(ROOT)]

from api.services.casting.yield_analysis import (  # noqa: E402
    _parse_mcp_rows,
    find_mssql_server,
)
from api.services.mcp.isolated_stdio import (  # noqa: E402
    run_execute_queries_isolated,
    snapshot_mcp_server,
)
from db.session import AsyncSessionLocal  # noqa: E402

# ---------------------------------------------------------------------------
# 只读探测 SQL（均可单独复制到 SSMS / MCP execute_query）
# ---------------------------------------------------------------------------

SQL_TABLES = """
SELECT TOP 80
  s.name AS SchemaName,
  t.name AS TableName
FROM sys.tables t
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name IN (N'qual', N'invn', N'sale', N'comn', N'app', N'make')
  AND (
    t.name LIKE N'%Bad%'
    OR t.name LIKE N'%Dictionary%'
    OR t.name LIKE N'%QualityFirst%'
    OR t.name LIKE N'%QualityObject%'
    OR t.name LIKE N'%SaleContract%'
    OR t.name LIKE N'%SaleOrder%'
    OR t.name LIKE N'%Materal%'
    OR t.name LIKE N'%MakingObject%'
  )
ORDER BY s.name, t.name
""".strip()

SQL_COLUMNS = """
SELECT TOP 200
  s.name AS SchemaName,
  t.name AS TableName,
  c.name AS ColumnName,
  ty.name AS DataType
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE (
    (s.name = N'qual' AND t.name LIKE N'%Bad%')
    OR (s.name = N'invn' AND t.name IN (
         N'QualityFirstStage', N'QualityObjectScrap', N'InventoryMakingObject'
    ))
    OR (s.name = N'sale' AND t.name IN (
         N'SaleContract', N'SaleContractItem', N'SaleOrder', N'SaleOrderItem'
    ))
    OR (s.name = N'comn' AND t.name LIKE N'%Materal%')
    OR (s.name = N'app' AND t.name LIKE N'%Dictionary%')
  )
ORDER BY s.name, t.name, c.column_id
""".strip()

SQL_BAD_DICT_PEEL = """
SELECT TOP 80
  CAST(b.BadDictionaryGUID AS nvarchar(36)) AS BadDictionaryGUID,
  b.BadDictionaryCode,
  b.BadDictionaryName
FROM qual.BadDictionary b
WHERE b.BadDictionaryName LIKE N'%脱角%'
   OR b.BadDictionaryCode LIKE N'%脱角%'
   OR b.BadDictionaryName LIKE N'%掉角%'
   OR b.BadDictionaryName LIKE N'%缺角%'
ORDER BY b.BadDictionaryCode
""".strip()

SQL_BAD_DICT_POS = """
SELECT TOP 80
  CAST(b.BadDictionaryGUID AS nvarchar(36)) AS BadDictionaryGUID,
  b.BadDictionaryCode,
  b.BadDictionaryName
FROM qual.BadDictionary b
WHERE b.BadDictionaryName LIKE N'%待口%'
   OR b.BadDictionaryName LIKE N'%带口%'
   OR b.BadDictionaryName LIKE N'%底部%'
   OR b.BadDictionaryName LIKE N'%底%'
   OR b.BadDictionaryName LIKE N'%面%'
   OR b.BadDictionaryName LIKE N'%脱角%'
ORDER BY b.BadDictionaryCode
""".strip()

SQL_Q1_CAUSE_PEEL = """
SELECT TOP 50
  ISNULL(q.BadDictionaryCodeList, N'(空)') AS BadDictionaryCodeList,
  ISNULL(q.QualityCauseDesc, N'(空)') AS QualityCauseDesc,
  COUNT(1) AS Cnt
FROM invn.QualityFirstStage q
WHERE ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%脱角%'
   OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%脱角%'
   OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%掉角%'
   OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%掉角%'
GROUP BY q.BadDictionaryCodeList, q.QualityCauseDesc
ORDER BY COUNT(1) DESC
""".strip()

SQL_ORDER_25091 = """
SELECT TOP 50
  CAST(o.SaleOrderGUID AS nvarchar(36)) AS SaleOrderGUID,
  o.SaleOrderCode,
  CONVERT(varchar(10), o.SaleOrderDate, 23) AS SaleOrderDate,
  o.SSaleOrderStatusKey
FROM sale.SaleOrder o
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
ORDER BY
  CASE WHEN o.SaleOrderCode = N'25091' THEN 0 ELSE 1 END,
  o.SaleOrderCode
""".strip()

SQL_CONTRACT_25091 = """
SELECT TOP 20
  CAST(c.SaleContractGUID AS nvarchar(36)) AS SaleContractGUID,
  c.SaleContractCode,
  CONVERT(varchar(10), c.SaleContractDate, 23) AS SaleContractDate
FROM sale.SaleContract c
WHERE c.SaleContractCode = N'25091'
   OR c.SaleContractCode LIKE N'25091%'
ORDER BY c.SaleContractCode
""".strip()

# 订单号 25091* 订货清单：按材质 / 浇筑键汇总规格数
SQL_PT_SPEC_SUMMARY = """
SELECT TOP 40
  ISNULL(mt.ProductMateralTypeName, N'(无材质)') AS MaterialName,
  ISNULL(mt.SCastingTypeKey, N'(无浇筑键)') AS CastingTypeKey,
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt,
  COUNT(1) AS ItemRowCnt,
  SUM(CAST(si.BillOrderQty AS float)) AS BillQty
FROM sale.SaleOrder o
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
GROUP BY mt.ProductMateralTypeName, mt.SCastingTypeKey
ORDER BY SpecCnt DESC
""".strip()

SQL_PT_SPEC_FILTERED = """
SELECT TOP 10
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN si.InventoryGUID
  END) AS SpecCnt_MaterialPT,
  COUNT(DISTINCT CASE
    WHEN mt.SCastingTypeKey LIKE N'%PT%' THEN si.InventoryGUID
  END) AS SpecCnt_CastKeyPT,
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%'
      OR mt.SCastingTypeKey LIKE N'%PT%'
    THEN si.InventoryGUID
  END) AS SpecCnt_EitherPT,
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt_AllItems,
  COUNT(DISTINCT o.SaleOrderGUID) AS OrderCnt,
  COUNT(1) AS ItemRowCnt
FROM sale.SaleOrder o
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
""".strip()

# 若存在销售合同表，按合同号再数一遍（与报表标题「25091合同」对齐）
SQL_PT_SPEC_BY_CONTRACT = """
SELECT TOP 10
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN ci.InventoryGUID
  END) AS SpecCnt_MaterialPT,
  COUNT(DISTINCT CASE
    WHEN mt.SCastingTypeKey LIKE N'%PT%' THEN ci.InventoryGUID
  END) AS SpecCnt_CastKeyPT,
  COUNT(DISTINCT ci.InventoryGUID) AS SpecCnt_AllItems,
  COUNT(DISTINCT c.SaleContractGUID) AS ContractCnt,
  COUNT(1) AS ItemRowCnt
FROM sale.SaleContract c
INNER JOIN sale.SaleContractItem ci ON ci.SaleContractGUID = c.SaleContractGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = ci.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'25091'
   OR c.SaleContractCode LIKE N'25091%'
""".strip()

SQL_MO_LINK_COLS = """
SELECT TOP 80
  c.name AS ColumnName,
  ty.name AS DataType
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE s.name = N'invn' AND t.name = N'InventoryMakingObject'
  AND (
    c.name LIKE N'%Sale%'
    OR c.name LIKE N'%Contract%'
    OR c.name LIKE N'%Order%'
    OR c.name LIKE N'%Bill%'
    OR c.name LIKE N'%Inventory%'
    OR c.name LIKE N'%Making%'
  )
ORDER BY c.column_id
""".strip()

# 一检脱角在 25091* 物料上的出现次数（按物料档案关联，可能含同型号其他合同件）
SQL_Q1_PEEL_ON_25091_INV = """
SELECT TOP 10
  COUNT(1) AS FirstInspectCnt,
  SUM(CASE
        WHEN ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%脱角%'
          OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%脱角%'
          OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%掉角%'
          OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%掉角%'
        THEN 1 ELSE 0 END) AS PeelCnt,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCntTouched
FROM invn.QualityFirstStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
WHERE mo.InventoryGUID IN (
  SELECT si.InventoryGUID
  FROM sale.SaleOrder o
  INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
  LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
  LEFT JOIN comn.ProductMateralType mt
    ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
  WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
    AND (
      mt.ProductMateralTypeName LIKE N'%PT%'
      OR mt.SCastingTypeKey LIKE N'%PT%'
    )
)
""".strip()

SQL_POS_ON_25091_PT = """
SELECT TOP 30
  ISNULL(inv.InventoryPosition, N'(空)') AS PositionName,
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt
FROM sale.SaleOrder o
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
INNER JOIN invn.Inventory inv ON inv.InventoryGUID = si.InventoryGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
  AND (
    mt.ProductMateralTypeName LIKE N'%PT%'
    OR mt.SCastingTypeKey LIKE N'%PT%'
  )
GROUP BY inv.InventoryPosition
ORDER BY SpecCnt DESC
""".strip()

# 用 NCHAR 避免 Windows 源文件编码把「脱角」传进 MCP 后对不上
SQL_BAD_ALL = """
SELECT TOP 200
  b.BadDictionaryCode,
  b.BadDictionaryName
FROM qual.BadDictionary b
ORDER BY b.BadDictionaryCode
""".strip()

SQL_BAD_BY_CODE = """
SELECT TOP 80
  b.BadDictionaryCode,
  b.BadDictionaryName
FROM qual.BadDictionary b
WHERE b.BadDictionaryCode IN (N'212', N'213')
   OR b.BadDictionaryCode LIKE N'212%'
   OR b.BadDictionaryCode LIKE N'213%'
   OR b.BadDictionaryName LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
   OR b.BadDictionaryName LIKE N'%' + NCHAR(25481)+NCHAR(35282) + N'%'
   OR b.BadDictionaryName LIKE N'%' + NCHAR(32570)+NCHAR(35282) + N'%'
ORDER BY b.BadDictionaryCode
""".strip()

SQL_Q1_COLS = """
SELECT TOP 80
  c.name AS ColumnName,
  ty.name AS DataType
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE s.name = N'invn' AND t.name = N'QualityFirstStage'
ORDER BY c.column_id
""".strip()

SQL_CONTRACT_COLS = """
SELECT TOP 80
  s.name AS SchemaName,
  t.name AS TableName,
  c.name AS ColumnName,
  ty.name AS DataType
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE s.name = N'sale' AND t.name LIKE N'SaleContract%'
ORDER BY t.name, c.column_id
""".strip()

SQL_Q1_CODELIST = """
SELECT TOP 50
  ISNULL(q.BadDictionaryCodeList, N'(空)') AS BadDictionaryCodeList,
  COUNT(1) AS Cnt
FROM invn.QualityFirstStage q
GROUP BY q.BadDictionaryCodeList
ORDER BY COUNT(1) DESC
""".strip()

SQL_Q1_PEEL_NCHAR = """
SELECT TOP 40
  ISNULL(q.BadDictionaryCodeList, N'(空)') AS BadDictionaryCodeList,
  ISNULL(q.QualityCauseDesc, N'(空)') AS QualityCauseDesc,
  COUNT(1) AS Cnt
FROM invn.QualityFirstStage q
WHERE ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
   OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
   OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'212%'
   OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'213%'
GROUP BY q.BadDictionaryCodeList, q.QualityCauseDesc
ORDER BY COUNT(1) DESC
""".strip()

SQL_ORDER_CNT = """
SELECT TOP 10
  COUNT(1) AS OrderCnt,
  SUM(CASE WHEN o.SaleOrderCode = N'25091' THEN 1 ELSE 0 END) AS Exact25091Cnt,
  SUM(CASE WHEN o.SaleOrderCode LIKE N'25091B%' THEN 1 ELSE 0 END) AS BOrderCnt,
  MIN(o.SaleOrderCode) AS MinCode,
  MAX(o.SaleOrderCode) AS MaxCode
FROM sale.SaleOrder o
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
""".strip()

SQL_PT_EXACT_PARENT = """
SELECT TOP 10
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt_All,
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN si.InventoryGUID
  END) AS SpecCnt_MaterialPT,
  COUNT(1) AS ItemRowCnt,
  SUM(CAST(si.BillOrderQty AS float)) AS BillQty
FROM sale.SaleOrder o
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE o.SaleOrderCode = N'25091'
""".strip()

# 正确合同口径：在制件 SuppliedBySaleOrderGUID → 25091 / 25091B*
SQL_PT_BY_SUPPLIED = """
SELECT TOP 20
  ISNULL(mt.ProductMateralTypeName, N'(无材质)') AS MaterialName,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt,
  COUNT(DISTINCT mo.MakingObjectGUID) AS ObjectCnt,
  SUM(CAST(mo.MakingObjectQty AS float)) AS ObjectQty
FROM invn.InventoryMakingObject mo
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = mo.SuppliedBySaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
GROUP BY mt.ProductMateralTypeName
ORDER BY SpecCnt DESC
""".strip()

SQL_PT_SUPPLIED_FILTERED = """
SELECT TOP 10
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN mo.InventoryGUID
  END) AS SpecCnt_MaterialPT,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt_All,
  COUNT(DISTINCT mo.MakingObjectGUID) AS ObjectCnt,
  COUNT(DISTINCT o.SaleOrderGUID) AS OrderCnt
FROM invn.InventoryMakingObject mo
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = mo.SuppliedBySaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE o.SaleOrderCode = N'25091'
   OR o.SaleOrderCode LIKE N'25091%'
""".strip()

SQL_Q1_PEEL_SUPPLIED = """
SELECT TOP 20
  ISNULL(q.BadDictionaryCodeList, N'(空)') AS BadDictionaryCodeList,
  COUNT(1) AS InspectCnt,
  SUM(CAST(q.Qty AS float)) AS QtySum,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt
FROM invn.QualityFirstStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = mo.SuppliedBySaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
  AND mt.ProductMateralTypeName LIKE N'%PT%'
GROUP BY q.BadDictionaryCodeList
ORDER BY InspectCnt DESC
""".strip()

SQL_Q1_PEEL_SUPPLIED_SUM = """
SELECT TOP 10
  COUNT(1) AS FirstInspectCnt,
  SUM(CAST(q.Qty AS float)) AS InspectQty,
  SUM(CASE
        WHEN ISNULL(q.BadDictionaryCodeList, N'') LIKE N'212%'
          OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'213%'
          OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
          OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
        THEN 1 ELSE 0 END) AS PeelLotCnt,
  SUM(CASE
        WHEN ISNULL(q.BadDictionaryCodeList, N'') LIKE N'212%'
          OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'213%'
          OR ISNULL(q.BadDictionaryCodeList, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
          OR ISNULL(q.QualityCauseDesc, N'') LIKE N'%' + NCHAR(33073)+NCHAR(35282) + N'%'
        THEN CAST(q.Qty AS float) ELSE 0 END) AS PeelQty,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCntTouched,
  MIN(CONVERT(varchar(10), q.InputDatetime, 23)) AS MinDate,
  MAX(CONVERT(varchar(10), q.InputDatetime, 23)) AS MaxDate
FROM invn.QualityFirstStage q
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = q.MakingObjectGUID
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = mo.SuppliedBySaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
  AND mt.ProductMateralTypeName LIKE N'%PT%'
""".strip()

SQL_ORDER_CONTRACT_COLS = """
SELECT TOP 40
  c.name AS ColumnName,
  ty.name AS DataType
FROM sys.columns c
INNER JOIN sys.tables t ON t.object_id = c.object_id
INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE s.name = N'sale' AND t.name = N'SaleOrder'
  AND (
    c.name LIKE N'%Contract%'
    OR c.name LIKE N'%SaleOrderCode%'
    OR c.name LIKE N'%Parent%'
    OR c.name LIKE N'%Src%'
  )
ORDER BY c.column_id
""".strip()

SQL_CONTRACT_HIT = """
SELECT TOP 20
  CAST(c.SaleContractGUID AS nvarchar(36)) AS SaleContractGUID,
  c.SaleContractCode,
  CONVERT(varchar(10), c.SigningDate, 23) AS SigningDate,
  c.BillOrderQty,
  c.SSaleContractStatusKey
FROM sale.SaleContract c
WHERE c.SaleContractCode = N'25091'
   OR c.SaleContractCode LIKE N'25091%'
ORDER BY c.SaleContractCode
""".strip()

SQL_CONTRACT_ITEM = """
SELECT TOP 20
  ISNULL(mt.ProductMateralTypeName, N'(无材质)') AS MaterialName,
  COUNT(1) AS ItemRowCnt,
  SUM(CAST(ci.Qty AS float)) AS QtySum
FROM sale.SaleContract c
INNER JOIN sale.SaleContractItem ci ON ci.SaleContractGUID = c.SaleContractGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = ci.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'25091'
   OR c.SaleContractCode LIKE N'25091%'
GROUP BY mt.ProductMateralTypeName
ORDER BY ItemRowCnt DESC
""".strip()

SQL_CAST_WINDOW_SPEC = """
SELECT TOP 10
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt_Casted,
  COUNT(DISTINCT bi.MakingObjectGUID) AS ObjectCnt_Casted,
  MIN(CONVERT(varchar(10), b.CastedDate, 23)) AS MinCastDate,
  MAX(CONVERT(varchar(10), b.CastedDate, 23)) AS MaxCastDate
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
INNER JOIN sale.SaleOrder o ON o.SaleOrderGUID = mo.SuppliedBySaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
  AND mt.ProductMateralTypeName LIKE N'%PT%'
  AND b.CastedDate IS NOT NULL
""".strip()

SQL_CAST_WINDOW_INV = """
SELECT TOP 10
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt_Casted,
  COUNT(DISTINCT bi.MakingObjectGUID) AS ObjectCnt_Casted
FROM make.WorkCastingInBoxItem bi
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.MakingObjectGUID = bi.MakingObjectGUID
WHERE b.CastedDate IS NOT NULL
  AND mo.InventoryGUID IN (
    SELECT si.InventoryGUID
    FROM sale.SaleOrder o
    INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
    LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
    LEFT JOIN comn.ProductMateralType mt
      ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
    WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091%')
      AND mt.ProductMateralTypeName LIKE N'%PT%'
  )
""".strip()

SQL_ORDER_ITEM_PT_DATES = """
SELECT TOP 10
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt,
  COUNT(DISTINCT o.SaleOrderGUID) AS OrderCnt,
  MIN(CONVERT(varchar(10), o.SaleOrderDate, 23)) AS MinOrderDate,
  MAX(CONVERT(varchar(10), o.SaleOrderDate, 23)) AS MaxOrderDate
FROM sale.SaleOrder o
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE (o.SaleOrderCode = N'25091' OR o.SaleOrderCode LIKE N'25091B%')
  AND mt.ProductMateralTypeName LIKE N'%PT%'
""".strip()

STEPS_P1: list[tuple[str, str]] = [
    ("tables", SQL_TABLES),
    ("columns", SQL_COLUMNS),
    ("bad_dict_peel", SQL_BAD_DICT_PEEL),
    ("bad_dict_pos", SQL_BAD_DICT_POS),
    ("q1_cause_peel", SQL_Q1_CAUSE_PEEL),
    ("order_25091", SQL_ORDER_25091),
    ("contract_25091", SQL_CONTRACT_25091),
    ("pt_spec_summary", SQL_PT_SPEC_SUMMARY),
    ("pt_spec_filtered", SQL_PT_SPEC_FILTERED),
    ("pt_spec_by_contract", SQL_PT_SPEC_BY_CONTRACT),
    ("mo_link_cols", SQL_MO_LINK_COLS),
    ("q1_peel_on_25091", SQL_Q1_PEEL_ON_25091_INV),
    ("pos_on_25091_pt", SQL_POS_ON_25091_PT),
]

STEPS_P2: list[tuple[str, str]] = [
    ("q1_cols", SQL_Q1_COLS),
    ("contract_cols", SQL_CONTRACT_COLS),
    ("bad_all", SQL_BAD_ALL),
    ("bad_by_code", SQL_BAD_BY_CODE),
    ("q1_codelist", SQL_Q1_CODELIST),
    ("q1_peel_nchar", SQL_Q1_PEEL_NCHAR),
    ("order_cnt", SQL_ORDER_CNT),
    ("pt_exact_parent", SQL_PT_EXACT_PARENT),
    ("pt_by_supplied", SQL_PT_BY_SUPPLIED),
    ("pt_supplied_filtered", SQL_PT_SUPPLIED_FILTERED),
    ("q1_peel_supplied", SQL_Q1_PEEL_SUPPLIED),
    ("q1_peel_supplied_sum", SQL_Q1_PEEL_SUPPLIED_SUM),
]

STEPS_P3: list[tuple[str, str]] = [
    ("order_contract_cols", SQL_ORDER_CONTRACT_COLS),
    ("contract_hit", SQL_CONTRACT_HIT),
    ("contract_item", SQL_CONTRACT_ITEM),
    ("cast_window_supplied", SQL_CAST_WINDOW_SPEC),
    ("cast_window_inv", SQL_CAST_WINDOW_INV),
    ("order_item_pt_b", SQL_ORDER_ITEM_PT_DATES),
]

SQL_PT_BY_CONTRACT_FK = """
SELECT TOP 20
  ISNULL(mt.ProductMateralTypeName, N'(无材质)') AS MaterialName,
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt,
  COUNT(DISTINCT o.SaleOrderGUID) AS OrderCnt,
  COUNT(1) AS ItemRowCnt,
  SUM(CAST(si.BillOrderQty AS float)) AS BillQty
FROM sale.SaleContract c
INNER JOIN sale.SaleOrder o ON o.SaleContractGUID = c.SaleContractGUID
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'25091'
GROUP BY mt.ProductMateralTypeName
ORDER BY SpecCnt DESC
""".strip()

SQL_PT_BY_CONTRACT_FK_SUM = """
SELECT TOP 10
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN si.InventoryGUID
  END) AS SpecCnt_MaterialPT,
  COUNT(DISTINCT si.InventoryGUID) AS SpecCnt_All,
  COUNT(DISTINCT o.SaleOrderGUID) AS OrderCnt,
  COUNT(1) AS ItemRowCnt
FROM sale.SaleContract c
INNER JOIN sale.SaleOrder o ON o.SaleContractGUID = c.SaleContractGUID
INNER JOIN sale.SaleOrderItem si ON si.SaleOrderGUID = o.SaleOrderGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = si.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'25091'
""".strip()

SQL_CAST_BY_CONTRACT_FK = """
SELECT TOP 10
  COUNT(DISTINCT CASE
    WHEN mt.ProductMateralTypeName LIKE N'%PT%' THEN mo.InventoryGUID
  END) AS SpecCnt_PT_Casted,
  COUNT(DISTINCT mo.InventoryGUID) AS SpecCnt_All_Casted,
  COUNT(DISTINCT bi.MakingObjectGUID) AS ObjectCnt_Casted,
  MIN(CONVERT(varchar(10), b.CastedDate, 23)) AS MinCastDate,
  MAX(CONVERT(varchar(10), b.CastedDate, 23)) AS MaxCastDate
FROM sale.SaleContract c
INNER JOIN sale.SaleOrder o ON o.SaleContractGUID = c.SaleContractGUID
INNER JOIN invn.InventoryMakingObject mo
  ON mo.SuppliedBySaleOrderGUID = o.SaleOrderGUID
INNER JOIN make.WorkCastingInBoxItem bi
  ON bi.MakingObjectGUID = mo.MakingObjectGUID
INNER JOIN make.WorkCastingInBox b
  ON b.WorkCastingInBoxGUID = bi.WorkCastingInBoxGUID
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = mo.InventoryGUID
LEFT JOIN comn.ProductMateralType mt
  ON mt.ProductMateralTypeGUID = p.ProductMateralTypeGUID
WHERE c.SaleContractCode = N'25091'
  AND b.CastedDate IS NOT NULL
""".strip()

STEPS_P4: list[tuple[str, str]] = [
    ("pt_by_contract_fk", SQL_PT_BY_CONTRACT_FK),
    ("pt_by_contract_fk_sum", SQL_PT_BY_CONTRACT_FK_SUM),
    ("cast_by_contract_fk", SQL_CAST_BY_CONTRACT_FK),
]

def _print_rows(
    step: str,
    rows: list[dict],
    raw: str,
    *,
    max_rows: int = 80,
    sink=None,
) -> None:
    out = sink or sys.stdout
    print(f"\n===== {step}  rows={len(rows)} =====", file=out)
    if not rows:
        preview = (raw or "").strip().replace("\r\n", "\n")[:800]
        print(preview or "(empty)", file=out)
        return
    cols = list(rows[0].keys())
    print(" | ".join(str(c) for c in cols), file=out)
    print("-" * min(160, 4 + sum(len(str(c)) + 3 for c in cols)), file=out)
    for row in rows[:max_rows]:
        print(" | ".join(str(row.get(c, ""))[:80] for c in cols), file=out)
    if len(rows) > max_rows:
        print(f"... +{len(rows) - max_rows} more", file=out)


async def _snapshot() -> dict:
    async with AsyncSessionLocal() as db:
        server = await find_mssql_server(db)
        print(f"mcp server={server.name} id={server.public_id}")
        return snapshot_mcp_server(server)


def main() -> int:
    import argparse
    import asyncio
    import io

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        nargs="?",
        default="p2",
        choices=("p1", "p2", "p3", "p4", "all"),
        help="p1=表结构, p2=脱角码, p3=合同窗口, p4=合同外键规格数",
    )
    args = parser.parse_args()
    if args.phase == "p1":
        steps = STEPS_P1
    elif args.phase == "p3":
        steps = STEPS_P3
    elif args.phase == "p4":
        steps = STEPS_P4
    elif args.phase == "all":
        steps = STEPS_P1 + STEPS_P2 + STEPS_P3 + STEPS_P4
    else:
        steps = STEPS_P2

    snap = asyncio.run(_snapshot())
    log_path = ROOT / "scripts" / f"_probe_peel_25091_{args.phase}.txt"
    buf = io.StringIO()
    ok_n = 0
    fail_n = 0
    for name, sql in steps:
        try:
            batch = run_execute_queries_isolated(
                server_snapshot=snap,
                steps=[(name, sql)],
                timeout_seconds=90.0,
            )
        except Exception as exc:  # noqa: BLE001
            fail_n += 1
            msg = f"\n===== {name}  FAIL =====\n{str(exc)[:1200]}"
            print(msg)
            print(msg, file=buf)
            continue
        block = batch.get(name) or {}
        raw = str(block.get("content") or block.get("raw") or "")
        rows = block.get("rows")
        if not isinstance(rows, list):
            rows = _parse_mcp_rows(raw)
        _print_rows(name, rows, raw)
        _print_rows(name, rows, raw, sink=buf)
        ok_n += 1

    summary = f"\nDONE ok={ok_n} fail={fail_n} / {len(steps)}"
    print(summary)
    print(summary, file=buf)
    log_path.write_text(buf.getvalue(), encoding="utf-8")
    print(f"utf8 log: {log_path}")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
