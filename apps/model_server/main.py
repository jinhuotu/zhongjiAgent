import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "model_server.app:app",
        host=settings.model_host,
        port=settings.model_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
