from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import ai
from common import __version__
from common.config import get_settings
from common.errors import AppError
from common.logging import setup_logging
from common.response import fail, ok


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=f"{settings.app_name}-ai",
        version=__version__,
        description="AI 能力层：问答/报告（配置来自 .env LLM_* / EMBEDDING_*）",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(exc.code, exc.msg),
        )

    @app.get("/health")
    async def health() -> dict:
        return ok(
            {
                "status": "up",
                "service": "zhongji-ai",
                "ready": settings.llm_configured(),
                "llm_model": settings.model_for_mode("fast"),
                "embedding_model": settings.embedding_model,
            }
        )

    # Same AI routes as main API (can run as dedicated process on AI_PORT)
    app.include_router(ai.router, prefix=settings.api_prefix)

    @app.post(f"{settings.api_prefix}/ai/reports/generate")
    async def report_placeholder() -> dict:
        return ok({"message": "AI report generation not implemented yet"})

    return app


app = create_app()
