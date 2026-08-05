# 窑炉历史工况 Excel 导入

## 数据源

- 目录：`data/raw/kiln/`（`.xls`）
- 文件名：`3#_{batch}_{YYYYMMDD}.xls`
- 窑 ID：`TC-03`（显示名 3# 车式窑）
- 每个文件两个 Sheet：`温度`、`压力`（按表头自动识别，非两种文件混放）

## 表

| 表 | 说明 |
|---|---|
| `furnaces` | 窑台账；导入时自动种子 `TC-03` |
| `kiln_process_samples` | 分钟级宽表；唯一键 `(kiln_code, ts)` |

温度区字段：`z1_*` … `z6_*`（temp / gas_flow / air_flow / air_valve）。  
压力字段：炉压/燃气压/助燃风压（设定/测量/输出）、燃气与助燃风瞬时/累计流量、氧含量。  
累计流量跨文件原样写入，可能重置。

## 命令

```powershell
poetry run python -m alembic upgrade head
poetry run python scripts/import_kiln_xls.py --dir data/raw/kiln --kiln TC-03
poetry run python scripts/import_kiln_xls.py --dir data/raw/kiln --dry-run --limit 2
```

依赖：`xlrd`、`pymysql`（写入用同步引擎）。

## 只读 API（已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/furnaces` | 窑列表 + 最新快照摘要 |
| GET | `/api/v1/furnaces/{code}` | 单窑详情 |
| GET | `/api/v1/furnaces/{code}/series?from&to&stepMinutes&limit` | 历史曲线 |
| GET | `/api/v1/furnaces/{code}/snapshot?at=` 或 `offsetMinutes=` | 快照 / 回放 |

前端：`aizhongjiweb` 的 `/furnaces`、`/realtime` 已对接（`src/lib/furnaces-api.ts`）。  
能碳总览：`GET /api/v1/overview` → 前端 `/`（`src/lib/overview-api.ts`）。
