import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    # DEBUG=true 时热重载；只监视业务代码，避免无关文件变更触发重启/退出
    reload_dirs = None
    if settings.debug:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]  # repo root (…/zhongjiAgent)
        reload_dirs = [
            str(root / "apps" / "api"),
            str(root / "packages"),
        ]
    uvicorn.run(
        "api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        reload_dirs=reload_dirs,
        # Avoid reload loops when poetry/pip touches site-packages
        reload_excludes=[
            ".venv/*",
            ".venv\\*",
            "**/site-packages/**",
            "**/.pytest_cache/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/alembic/versions/**",
        ],
    )


if __name__ == "__main__":
    run()
