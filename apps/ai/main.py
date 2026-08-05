import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "ai.app:app",
        host=settings.ai_host,
        port=settings.ai_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
