from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

from api.routers import (
    agents,
    ai,
    auth,
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
    roles,
    users,
)
from common import __version__
from common.config import get_settings
from common.errors import AppError
from common.logging import setup_logging
from common.response import fail


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
    )

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

    return app


app = create_app()
