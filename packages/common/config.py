from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "zhongji-agent"
    app_env: str = "dev"
    debug: bool = True
    api_prefix: str = "/api/v1"
    # "*" = allow any Origin (dev default). Production: set explicit frontend URLs.
    cors_origins: str = "*"

    api_host: str = "0.0.0.0"
    api_port: int = 8800
    ai_host: str = "0.0.0.0"
    ai_port: int = 8001
    ingest_host: str = "0.0.0.0"
    ingest_port: int = 8002
    model_host: str = "0.0.0.0"
    model_port: int = 8003

    database_url: str = (
        "mysql+asyncmy://zhongji:zhongji_dev@127.0.0.1:3306/zhongji_agent"
    )
    # Docker Compose 默认映射宿主机 16379 → 容器 6379
    redis_url: str = "redis://127.0.0.1:16379/0"

    # ---- Chat hot memory / Stream archive (M1) ----
    chat_session_ttl_seconds: int = 7 * 24 * 3600
    chat_cold_start_turns: int = 15
    chat_stream_key: str = "agent:chat:stream"
    chat_stream_dlq_key: str = "agent:chat:stream:dql"
    chat_stream_group: str = "chat-archiver"
    chat_stream_consumer_prefix: str = "worker"
    chat_stream_batch_size: int = 30
    chat_stream_block_ms: int = 5000

    # ---- Chat M2：裁剪 / 限流 / 锁 / 缓存 / 长期记忆 / 热配置 ----
    chat_trim_trigger_turns: int = 25
    chat_trim_keep_turns: int = 10
    chat_rate_limit_max: int = 30
    chat_rate_limit_window_seconds: int = 60
    # MCP stdio 启停 + 多轮 tool calling 可能超过 2 分钟；配合续租避免锁提前过期
    chat_session_lock_ttl_seconds: int = 300
    chat_embed_cache_ttl_seconds: int = 7 * 24 * 3600
    chat_memory_collection: str = "lujing_chat_memory"
    chat_ttl_scan_threshold_seconds: int = 3600
    chat_ttl_scan_cron: str = "0 3 * * *"  # 每天 03:00
    hot_config_hash_key: str = "hot:config"
    # 操作日志 / 登录日志滚动保留天数（超时删除，系统最多保留该窗口）
    audit_log_retention_days: int = 7

    jwt_secret_key: str = "change-me-in-production-use-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7

    storage_root: str = "./storage"

    # MCP stdio 仓库根（可选）。不设则自动探测 scripts/mcp_utility_server.py 所在根目录。
    # 部署非标准目录结构时可设：ZHONGJI_ROOT=/opt/zhongjiAgent
    zhongji_root: str = ""

    # ---- LLM (AI 智能问答 / 报告) OpenAI-compatible ----
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_model_fast: str = ""
    llm_model_deep: str = ""
    llm_temperature_fast: float = 0.6
    llm_temperature_deep: float = 0.4
    llm_timeout_seconds: float = 120.0

    # ---- Embedding (知识库向量化) OpenAI-compatible ----
    # Prefer EMBEDDING_*; if empty, fall back to LLM_* for same gateway.
    # If still empty, use local-hash (offline MVP only).
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_allow_local_fallback: bool = True
    # 远端 embeddings 单次 input 条数；过大易被 MaaS 以 400 拒绝
    embedding_batch_size: int = 8
    # 单条文本字符上限（防御超长块）；中文手册切块默认 800，此值作兜底
    embedding_max_chars: int = 6000

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "lujing_knowledge"

    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    kb_search_top_k: int = 5
    kb_search_min_score: float = 0.0
    # 向量召回候选倍数，再按关键词重排截断为 top_k
    kb_search_candidate_multiplier: int = 4
    # 混合分 = (1-w)*向量分 + w*关键词分；停电/禁令等短查询建议 0.3～0.45
    kb_search_keyword_weight: float = 0.4

    # ---- Knowledge video / ASR ----
    kb_upload_max_bytes: int = 200 * 1024 * 1024
    kb_video_upload_max_bytes: int = 512 * 1024 * 1024
    kb_video_asr_timeout_seconds: int = 7200
    asr_provider: str = "none"
    asr_api_base: str = ""
    asr_api_key: str = ""
    asr_model: str = "whisper-1"
    asr_language: str = "zh"

    # ---- Knowledge image / OCR ----
    ocr_provider: str = "none"
    ocr_endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com"
    ocr_access_key_id: str = ""
    ocr_access_key_secret: str = ""
    ocr_type: str = "Advanced"
    ocr_timeout_seconds: int = 120

    @property
    def cors_origin_list(self) -> list[str]:
        """Resolve CORS allow_origins.

        - empty / ``*`` → allow any Origin (Starlette echoes Origin when credentials=True)
        - comma-separated list → whitelist (set this in production)
        """
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]

    @property
    def resolved_llm_api_base(self) -> str:
        return (self.llm_api_base or "").rstrip("/")

    @property
    def resolved_embedding_api_base(self) -> str:
        return (self.embedding_api_base or self.llm_api_base or "").rstrip("/")

    @property
    def resolved_embedding_api_key(self) -> str:
        return self.embedding_api_key or self.llm_api_key or ""

    def model_for_mode(self, mode: str) -> str:
        if mode == "deep":
            return self.llm_model_deep or self.llm_model
        return self.llm_model_fast or self.llm_model

    def temperature_for_mode(self, mode: str) -> float:
        if mode == "deep":
            return self.llm_temperature_deep
        return self.llm_temperature_fast

    def llm_configured(self) -> bool:
        return bool(self.resolved_llm_api_base and self.llm_api_key)

    def embedding_configured(self) -> bool:
        return bool(self.resolved_embedding_api_base and self.resolved_embedding_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
