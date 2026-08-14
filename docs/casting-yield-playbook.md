# 铸造同型号良率最优文档（落地说明）

| 项 | 内容 |
|---|---|
| 文档版本 | V1.8 |
| 更新日期 | 2026-08-13 |
| 入口键 | **订单编号** `sale.SaleOrder.SaleOrderCode`；点物料后再用 **`InventoryGUID`** 做同型号分析 |
| 访问 | `docs/casting-yield-playbook.md` |

---

## 1. 需求（当前交付口径）

员工在前端输入 **订单编号**（两层）：

| 结果 | 行为 |
|---|---|
| 订单不存在 | 返回：没有该订单编号 |
| 多条订单命中 | 返回订单候选，点选后加载订货清单 |
| 唯一订单 | 列出本单物料（编码 / 名称 / 规格 / 材质 / 合同数量 / 计划数量 / 在制 / 库存 / 已排产） |
| 点某一物料「分析」 | 按该物料 `InventoryGUID` 跑同型号历史库存、**组合良率（浇铸在制件→二检）**、班组二检对照、工序明细、厂区历史气温，并可导出 Markdown |

历史气温为**必选**；厂区坐标优先读 MCP「通用工具（时间/天气）」的 `FACTORY_LAT` / `FACTORY_LON` / `FACTORY_NAME`（例如远东耐材）。

---

## 2. 接口

```http
POST /api/v1/casting/order-search
POST /api/v1/casting/order-items
POST /api/v1/casting/inventory-search
POST /api/v1/casting/yield-analysis
POST /api/v1/casting/yield-analysis/stream
POST /api/v1/casting/yield-document
POST /api/v1/casting/yield-document/stream
```

`/stream` 为 SSE，事件：`progress` → `done` / `error`。

「查询订单」只查 `sale.SaleOrder` / `sale.SaleOrderItem`。「分析」才跑 MES + 气温。分析结果页的「导出文档」把分析表格原样写入 Markdown，模型只补结论与建议，**不再让 LLM 重写数字**，也**不再重复查 MES**。文档写入优先本机直写 filesystem 允许目录，避免 Windows stdio MCP 卡住。

### 代码位置

| 文件 | 作用 |
|---|---|
| `apps/api/routers/casting.py` | 路由（含 SSE） |
| `apps/api/schemas/casting.py` | 入参 |
| `apps/api/services/casting/yield_analysis.py` | MES 分析、工序明细、气温-良率分箱、文档生成 |
| `apps/api/services/casting/weather.py` | Open-Meteo 历史气温；厂区坐标 |
| `scripts/seed_casting_prompts.py` | 提示词种子 |
| 前端 `zhongjivueweb`：`/casting-yield` | 联想、分步提示、工序表、复制/下载 |

### 分析响应要点

`found` / `needSelect` / `candidates` / `inventory`（含部位、尺寸、冒口规格等） / `stockSummary` / `lines` / `bestLine` / `processSummary` / `weatherSummary` / `weatherYield` / `rawContext` / `warnings`

文档响应另含 `markdown`、`fileExport`。

---

## 3. 真实表关联

```
sale.SaleOrder（订单编号 SaleOrderCode）
  └ sale.SaleOrderItem（BillOrderQty / ScheduOrderQty / ThisWorkingQty / ThisStockQty / ThisMPSingQty）
        └ invn.Inventory（本单物料）

invn.Inventory
  └ invn.Inventory_Product（尺寸 / 冒口物料 / 材质类型GUID / 产品类型GUID）
        ├ comn.ProductMateralType（材质名如 M-PT，SCastingTypeKey）
        ├ comn.ProductType（产品类型名）
        ├ make.vw_MPSItem.SCastingTypeName（浇筑类型如 PT/WS/YS）
        └ 冒口物料再 join invn.Inventory（冒口名称、规格如 φ250）

invn.InventoryMakingObject
  ├ make.WorkPlanSandItem / WorkPlanSand     砂型班计划：配给量、配给重量、完工量、班别
  ├ make.WorkMakingObject                    打型 SandBuild* / 粘型 SandPaste*
  ├ make.WorkCastingFurnaceItem / Furnace    电炉计划（FurnaceNo / CastingTime）
  ├ make.WorkCastingPlan                     浇铸计划（经电炉头 WorkCastingPlanGUID）
  ├ make.WorkCastingInBoxItem / WorkCastingInBox
  │     浇铸任务按在制件列出：月炉次 FurnaceNo / 箱号退火箱号 / 班炉次 FurnaceSerialNo /
  │     班别 WorkShift / 电炉 FurnaceName / 节点退火窖位 / 规格部位材质浇铸类型
  ├ invn.QualityFirstStage                   一检
  ├ make.WorkCutRiserPlanItem / Plan         切冒（多为浇筑类型 WS）
  ├ make.WorkMachingModifyPlanItem / Plan    改型（SrcMakingObjectGUID）
  ├ invn.QualityCutRiserStage                切检
  ├ make.WorkMachingDailyPlanItem / Plan     加工日计划
  ├ make.WorkMachingQualityOperation         加工任务（QualityDate）
  ├ invn.QualitySecondStage                  二检班组良率 + 分日良率
  ├ invn.QualityObjectScrap                  实物报废
  └ invn.QualityFinalStage                   终检（无 Qty，按件计 1）
        └ make.WorkGroup
```

**工艺行展示口径：** 查询页分 **砂型作业 / 熔铸作业 / 加工作业** 三大类展开。砂型班计划按「日期 + 班别 + 计划号」；打型/粘型按完工日 + 班组；组型/浇铸/出箱共用 `WorkCastingInBox` 不同日期列。电炉计划行内带 **材质**（炉次头 `ProductMateralType`，空则回退物料档案）；浇铸计划按本型号挂上的月炉次分行（`WorkCastingFurnace.FurnaceNo`），并带电炉；组型任务按日+班组+炉次+电炉汇总，行内带炉次/电炉/部位/冒重/毛重/冒口规格。出箱任务、浇铸任务行内带 **保温天数** = `DATEDIFF(day, CastedDate, ActualOutDate)`（未出箱为空，不用登记天数或计划出箱日）。材质/浇筑类型/冒口补到行内。`make.WorkSandTask` 在现库不存在，打型/粘型走 `WorkMakingObject`。组装作业（组装计划/打箱）本阶段未纳入。

合格键 `SIdentificationResultKey`：空或以 `1` 开头 = 合格；以 `2` 开头 = 报废；良率 = 合格 / 投入。产线维为班组 `WorkGroup`。

---

## 4. 前端用法

路径：`/casting-yield`（AI 智控菜单）

1. 输入订单编号时边打边出候选，点选即加载订货清单。
2. **查询订单**：本单物料表（合同/计划数量与本单在制、库存、已排产）。
3. **分析**（某一物料）：档案、**查询解读（结论 / 推荐安排 / 缺陷预防 / 风险）**、库存、班组良率、工序表按三大类展开、气温-良率分箱。无记录的工序仍占位显示「无工艺行」。
4. **导出文档**：表格（订单本行、物料、库存、良率、工序、气温）**原样来自本次分析**，模型只写结论/建议，避免改写数字。文档会标明：良率为该物料同型号历史数据，不是仅本订单。写入 filesystem MCP 允许目录。本阶段不写入知识库。

工作流节点 `yield_analysis`、场景智能体 / 知识库入库：**下一阶段再做**，本页主路径不依赖它们。

---

## 5. 气温与良率

- 厂区：MCP 天气工具 env → 进程 `FACTORY_*` → 郑州兜底（兜底会在 warnings 里标明）。
- **组合良率**（优先）：同一在制件的浇铸任务（班别 × 电炉 × 月炉次 × 箱号）关联最终二检；投入≥3 才作为推荐组合。
- **班组二检排名**（对照）：不拆浇铸属性。
- **浇铸日分箱**（观察）：浇铸日期 × 该在制件最终二检 × 当日厂区均温。
- **二检日分箱**（观察）：二检录入日气温 × 当日良率。
- 分箱区间：`<0℃` / `0–10℃` / `10–20℃` / `20–30℃` / `≥30℃`。样本不足时不得当因果。

---

## 6. 提示词

```powershell
venv\Scripts\python.exe scripts\seed_casting_prompts.py
```

会创建或**更新**「铸造同型号良率最优文档」「铸造同型号排产建议」。导出文档时表格由分析上下文原样附上，提示词只约束结论/建议小节；GUID 类字段不得编中文名。

---

## 7. 验收

| 场景 | 期望 |
|---|---|
| 不存在的订单编号 | 明确文案「没有该订单编号」 |
| `25120311` 等唯一订单 | 订货清单物料表 |
| 点某一物料「分析」 | 该型号库存 + 班组良率 + 三大类工序表 + 气温 |
| 导出文档 | 本页 Markdown 预览 + 本机文件；表格数字与查询分析一致；文首含本次订单编号 |
| 天气工具已配远东耐材坐标 | 气温摘要显示该厂名与经纬度，而不是郑州示例 |
