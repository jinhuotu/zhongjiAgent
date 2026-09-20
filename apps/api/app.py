from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

from api.middleware import JwtAuthMiddleware, OperationLogMiddleware
from api.routers import (
    agents,
    ai,
    alerts,
    audit,
    auth,
    casting,
    furnaces,
    governance,
    health,
    hot_configs,
    knowledge,
    mcp_servers,
    models,
    overview,
    production,
    prompts,
    reports,
    roles,
    users,
    workflows,
)
from common import __version__
from common.config import get_settings
from common.errors import AppError
from common.logging import setup_logging
from common.response import fail


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        from api.services import audit as audit_svc

        deleted = await audit_svc.purge_expired_logs()
        logging.getLogger("api.app").info(
            "audit purge on startup operations=%s logins=%s",
            deleted.get("operations", 0),
            deleted.get("logins", 0),
        )
    except Exception:  # noqa: BLE001
        logging.getLogger("api.app").exception("audit purge on startup failed")
    try:
        from api.services.knowledge.video import reclaim_parsing_videos, start_video_worker

        start_video_worker()
        n = await reclaim_parsing_videos()
        logging.getLogger("api.app").info("kb video worker started requeued=%s", n)
    except Exception:  # noqa: BLE001
        logging.getLogger("api.app").exception("kb video worker start failed")
    try:
        from api.services.knowledge.image import reclaim_parsing_images, start_image_worker

        start_image_worker()
        n = await reclaim_parsing_images()
        logging.getLogger("api.app").info("kb image worker started requeued=%s", n)
    except Exception:  # noqa: BLE001
        logging.getLogger("api.app").exception("kb image worker start failed")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="工业燃气车式窑数字化能碳管控平台 - 主业务 API",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    # 先注册 JWT（内侧），再注册操作日志，最后 CORS（外侧），保证 401 响应也带 CORS 头
    app.add_middleware(JwtAuthMiddleware)
    app.add_middleware(OperationLogMiddleware)
    # CORS: default "*" allows any Origin. For production set CORS_ORIGINS to frontend URLs.
    # With allow_credentials=True, Starlette echoes the request Origin instead of literal "*".
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        # 5xx 业务错误也落日志，避免「只有浏览器 502、后端像没反应」
        if exc.status_code >= 500:
            logging.getLogger("api.app").error(
                "AppError %s: %s", exc.status_code, exc.msg
            )
        elif exc.status_code >= 400:
            logging.getLogger("api.app").warning(
                "AppError %s: %s", exc.status_code, exc.msg
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(exc.code, exc.msg),
        )

    # 公开健康检查（根路径 + 版本前缀）
    app.include_router(health.router)
    app.include_router(health.router, prefix=settings.api_prefix)
    # 业务接口统一 /api/v1
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(users.router, prefix=settings.api_prefix)
    app.include_router(roles.router, prefix=settings.api_prefix)
    app.include_router(audit.router, prefix=settings.api_prefix)
    app.include_router(knowledge.router, prefix=settings.api_prefix)
    app.include_router(ai.router, prefix=settings.api_prefix)
    app.include_router(models.router, prefix=settings.api_prefix)
    app.include_router(mcp_servers.router, prefix=settings.api_prefix)
    app.include_router(prompts.router, prefix=settings.api_prefix)
    app.include_router(agents.router, prefix=settings.api_prefix)
    app.include_router(hot_configs.router, prefix=settings.api_prefix)
    app.include_router(furnaces.router, prefix=settings.api_prefix)
    app.include_router(overview.router, prefix=settings.api_prefix)
    app.include_router(production.router, prefix=settings.api_prefix)
    app.include_router(governance.router, prefix=settings.api_prefix)
    app.include_router(alerts.router, prefix=settings.api_prefix)
    app.include_router(reports.router, prefix=settings.api_prefix)
    app.include_router(workflows.router, prefix=settings.api_prefix)
    app.include_router(casting.router, prefix=settings.api_prefix)

    logging.getLogger("api.app").info(
        "casting routes: %s",
        [
            getattr(r, "path", "")
            for r in casting.router.routes
            if getattr(r, "path", "")
        ],
    )

    return app


app = create_app()
