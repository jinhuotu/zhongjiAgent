import logging
import sys

import uvicorn

from common.config import get_settings


def run() -> None:
    settings = get_settings()
    # DEBUG=true 时热重载；只监视业务代码，避免无关文件变更触发重启/退出
    # Windows 下 uvicorn reload 会再拉一个系统 Python 子进程，且常和旧进程一起占 8000，
    # 请求打到旧进程就会出现「源码有路由、线上 404」。本地 Windows 改为进程内加载，改代码请重启 API。
    use_reload = bool(settings.debug) and sys.platform != "win32"
    reload_dirs = None
    if use_reload:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]  # repo root (…/zhongjiAgent)
        reload_dirs = [
            str(root / "apps" / "api"),
            str(root / "packages"),
        ]
    if settings.debug and not use_reload:
        logging.getLogger("api.main").warning(
            "Windows 已关闭 uvicorn --reload，避免 8000 上残留旧进程导致新接口 404；改代码后请重启 API"
        )
    uvicorn.run(
        "api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=use_reload,
        reload_dirs=reload_dirs,
        # Avoid reload loops when poetry/pip touches site-packages
        reload_delay=1.0,
        reload_excludes=[
            ".venv/*",
            ".venv\\*",
            "venv/*",
            "venv\\*",
            "**/site-packages/**",
            "**/.pytest_cache/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/alembic/versions/**",
        ],
    )


if __name__ == "__main__":
    run()
