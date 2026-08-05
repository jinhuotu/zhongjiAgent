import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        # Avoid reload loops when poetry/pip touches site-packages
        reload_excludes=[".venv/*", ".venv\\*", "**/site-packages/**", "**/.pytest_cache/**"],
    )


if __name__ == "__main__":
    run()
