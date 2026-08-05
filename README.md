# 工业燃气车式窑数字化能碳管控平台 — 后端（zhongji-agent）

Poetry 单体多模块后端。前端工程：`aizhongjiweb`（默认联调 `http://127.0.0.1:8000`）。

详细现状与接口说明见：

- [`docs/开发文档.md`](docs/开发文档.md)
- [`docs/chat-memory.md`](docs/chat-memory.md)（对话 Redis 热记忆 / Stream 归档）
- [`docs/生产部署.md`](docs/生产部署.md)（轻量云主机生产部署与排障）

## 目录结构

```
apps/
  api/             # 主业务 FastAPI（:8000）
  ai/              # AI 侧车占位（:8001）
  ingest/          # 采集占位（:8002）
  model_server/    # ONNX 占位（:8003）
packages/
  common/          # 配置、JWT、Redis、统一响应
  db/              # SQLAlchemy + Alembic
workers/
  stream_consumer_main.py   # 对话 Stream 归档 + TTL 定时兜底
  dev_all.py                # 本地一键起 API+Worker
deploy/
  docker-compose.yml        # MySQL / Redis(16379) / Qdrant / chat-consumer-worker
docs/
```

## 快速开始

```powershell
poetry install
copy .env.example .env
# 编辑 .env：DATABASE_URL、REDIS_URL（默认 redis://127.0.0.1:16379/0）

# 基础设施（本机已占 3306/6379 时，用 compose 的 Redis 16379 + 本机 MySQL 即可）
docker compose -f deploy/docker-compose.yml up -d redis qdrant

poetry run python -m alembic upgrade head
poetry run python scripts/seed_admin.py
# 默认管理员：admin / Admin@123456
```

### 启动后端（二选一）

**推荐本地联调（一条命令起 API + Worker）：**

```powershell
poetry run zhongji-dev
```

**或分两个终端（生产形态一致）：**

```powershell
poetry run zhongji-api          # 主 API :8000
poetry run zhongji-chat-worker  # 对话异步归档（测智能问答时必开）
```

| 命令 | 作用 |
|---|---|
| `zhongji-api` | HTTP API：登录、用户/角色、知识库、模型、AI 对话 SSE |
| `zhongji-chat-worker` | Redis Stream → MySQL 归档；APScheduler TTL 兜底 |
| `zhongji-dev` | **仅本地**：同时拉起上面两个子进程；Ctrl+C 一起停 |

> 生产 / Docker 请分容器部署，不要把 Worker 嵌进 FastAPI BackgroundTasks。  
> Compose Worker：`docker compose -f deploy/docker-compose.yml --profile workers up -d chat-consumer-worker`

Swagger：http://127.0.0.1:8000/docs

### 前端

```powershell
# aizhongjiweb/.env.local
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
pnpm dev
```

已对接页面：`/login`、`/ai-chat`、`/knowledge`、`/model-manage`、`/users`（管理员）。

## 已具备能力（摘要）

- JWT 登录 / 用户与角色管理 / 模型管理（DB 配置）
- 知识库 RAG（MySQL 元数据 + Qdrant）
- AI 对话：服务端 Redis 窗口上下文 + SSE；异步 Stream 落库
- 对话 M2：限流、会话锁、Embedding 缓存、滚动裁剪、长期记忆摘要、热配置 Hash、TTL 兜底

## 窑炉历史工况导入（3# / TC-03）

Excel（`.xls`，每文件含「温度」「压力」两 Sheet）放在 `data/raw/kiln/`：

```powershell
poetry run python -m alembic upgrade head
poetry run python scripts/import_kiln_xls.py --dir data/raw/kiln --kiln TC-03
# 预览不写库：加 --dry-run
```

幂等键 `(kiln_code, ts)`，可重复执行。累计流量按文件原样入库（跨文件可能重置）。

## 明确未做 / 仍 mock

能碳业务 API、炉窑/实时前端对接、APS、质量、故障、AI 报告（前端仍走 Coze BFF）、真实 MCP 插件、采集与 ONNX 推理。
