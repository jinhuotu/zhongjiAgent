import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "ingest.app:app",
        host=settings.ingest_host,
        port=settings.ingest_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
