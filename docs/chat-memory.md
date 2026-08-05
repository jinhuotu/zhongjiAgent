# 对话热记忆与异步归档（M1 + M2）

> 总览与前后端矩阵见 [`开发文档.md`](./开发文档.md)。本地一键起 API+Worker：`poetry run zhongji-dev`。

## 数据分层

| 层 | 存储 | 职责 |
|---|---|---|
| 热数据 | Redis List `chat:session:{sessionId}` | 活跃会话上下文，TTL 7 天 |
| 队列 | Redis Stream `agent:chat:stream` | 待归档；DLQ：`agent:chat:stream:dql` |
| 冷数据 | MySQL `chat_messages` / `chat_sessions` | 唯一可信归档；审计与历史回看 |

接口层只保证：**List RPUSH + Stream XADD** 成功。MySQL 由独立 Worker 最终一致写入（秒级～1 分钟）。

## Redis Key 规范

| Key | 类型 | 说明 |
|---|---|---|
| `chat:session:{session_public_id}` | List | JSON 消息；TTL=`CHAT_SESSION_TTL_SECONDS`（默认 604800） |
| `agent:chat:stream` | Stream | 归档队列 |
| `agent:chat:stream:dql` | Stream | 死信队列 |

消息 JSON 字段：`id, role, content, mode, refs, modelName, promptTokens, completionTokens, totalTokens, toolName, toolInput, toolOutput, toolError, toolDurationMs, createdAt`

`role` 支持：`system` / `user` / `assistant` / `tool`（MCP 预留）。

## MySQL 扩展字段（`chat_messages`）

| 字段 | 说明 |
|---|---|
| `stream_msg_id` | Stream 消息 ID，唯一幂等键 |
| `model_name` | 模型名 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | Token 统计预留 |
| `tool_*` | MCP 工具调用预留 |

## 冷启动

1. 前端打开会话 / 发消息前：若 Redis List 不存在  
2. 从 MySQL 取最近 `CHAT_COLD_START_TURNS`（默认 15）轮（一对 user+assistant）  
3. 回填 Redis，供 LLM 使用  

历史会话全量查看：读 MySQL + 合并 Redis 未入库尾部。

## 环境变量

```env
REDIS_URL=redis://127.0.0.1:16379/0
CHAT_SESSION_TTL_SECONDS=604800
CHAT_COLD_START_TURNS=15
CHAT_STREAM_KEY=agent:chat:stream
CHAT_STREAM_DLQ_KEY=agent:chat:stream:dql
CHAT_STREAM_GROUP=chat-archiver
CHAT_STREAM_BATCH_SIZE=30
CHAT_STREAM_BLOCK_MS=5000
```

## 启动

```powershell
# 基础设施（Redis 映射 16379）
docker compose -f deploy/docker-compose.yml up -d redis qdrant

# 迁移
poetry run python -m alembic upgrade head

# 本地推荐：一条命令起 API + Worker（内部仍是两个子进程）
poetry run zhongji-dev

# 或分终端（生产形态一致）
# poetry run zhongji-api
# poetry run zhongji-chat-worker
```

**不要把归档逻辑挂进 FastAPI BackgroundTasks。**  
Compose：`docker compose -f deploy/docker-compose.yml --profile workers up -d chat-consumer-worker`（容器内访问宿主机 MySQL 时，`DATABASE_URL` 主机用 `host.docker.internal`）。

## 前端约定

`POST /api/v1/ai/chat` 请求体：

```json
{ "sessionId": "...", "content": "本轮用户问题", "mode": "fast" }
```

不再传完整 `messages` 历史。

## M2 已实现

| 能力 | Key / 说明 |
|---|---|
| 滑动窗口限流 | `ratelimit:chat:{userId}` ZSet；超限 HTTP 429 |
| 会话锁 | `lock:chat:session:{sessionId}` SET NX EX，防并发重复调 LLM |
| Embedding 缓存 | `embed:cache:{sha256}` String JSON 向量 |
| 滚动裁剪 | 超过 25 轮保留最近 10 轮；旧对话摘要写入 Qdrant `lujing_chat_memory` |
| 长期记忆召回 | 对话时检索同用户/会话摘要注入 Prompt |
| 热配置 | MySQL `hot_configs` + `hot_config_audits`；Redis Hash `hot:config` |
| TTL 兜底 | Worker 内 APScheduler（默认每天 03:00）扫描 TTL&lt;1h 会话补入库 |

### 热配置 API（管理员）

```http
GET    /api/v1/hot-configs
PUT    /api/v1/hot-configs
DELETE /api/v1/hot-configs/{key}
GET    /api/v1/hot-configs/audits
GET    /api/v1/hot-configs/value/{key}
```

内置键：`prompt.system_base`、`prompt.system_enabled`（首次 list 时自动种子）。

### M2 环境变量（节选）

```env
CHAT_TRIM_TRIGGER_TURNS=25
CHAT_TRIM_KEEP_TURNS=10
CHAT_RATE_LIMIT_MAX=30
CHAT_RATE_LIMIT_WINDOW_SECONDS=60
CHAT_MEMORY_COLLECTION=lujing_chat_memory
CHAT_TTL_SCAN_CRON=0 3 * * *
HOT_CONFIG_HASH_KEY=hot:config
```

## 后续迭代（未做）

- 真实 MCP 插件接入与 tool 指标看板  
- 多节点 Redlock（当前为单 Redis 互斥锁，满足同 Redis 多 API 实例）  
- 前端热配置管理页  
