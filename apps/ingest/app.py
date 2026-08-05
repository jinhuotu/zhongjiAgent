from fastapi import FastAPI

from common import __version__
from common.config import get_settings
from common.logging import setup_logging
from common.response import ok


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=f"{settings.app_name}-ingest",
        version=__version__,
        description="工业协议采集服务占位（本期不接 InfluxDB）",
    )

    @app.get("/health")
    async def health() -> dict:
        return ok(
            {
                "status": "up",
                "service": "zhongji-ingest",
                "ready": False,
                "note": "OPC UA / Modbus / MQTT adapters not implemented yet",
            }
        )

    return app


app = create_app()
