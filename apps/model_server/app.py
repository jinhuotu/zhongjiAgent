from fastapi import FastAPI

from common import __version__
from common.config import get_settings
from common.logging import setup_logging
from common.response import ok


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=f"{settings.app_name}-model",
        version=__version__,
        description="小模型 ONNX 推理服务占位",
    )

    @app.get("/health")
    async def health() -> dict:
        return ok({"status": "up", "service": "zhongji-model", "ready": False})

    @app.post(f"{settings.api_prefix}/models/energy/predict")
    async def energy_predict_placeholder() -> dict:
        return ok({"message": "energy predict not implemented yet"})

    @app.post(f"{settings.api_prefix}/models/temperature/predict")
    async def temperature_predict_placeholder() -> dict:
        return ok({"message": "temperature predict not implemented yet"})

    return app


app = create_app()
