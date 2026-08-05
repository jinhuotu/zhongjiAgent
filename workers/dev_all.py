"""本地开发一键启动：主 API + 对话归档 Worker。

用法：
  poetry run zhongji-dev

说明：
  - 仅方便本地联调，内部仍是两个独立子进程
  - 生产 / Docker 请继续分别部署 zhongji-api 与 zhongji-chat-worker
  - Ctrl+C 会同时结束两个子进程
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    env = os.environ.copy()
    sep = os.pathsep
    py_paths = [
        str(ROOT / "packages"),
        str(ROOT / "apps"),
        str(ROOT),
    ]
    env["PYTHONPATH"] = sep.join(py_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    api_cmd = [sys.executable, "-c", "from api.main import run; run()"]
    worker_cmd = [sys.executable, str(ROOT / "workers" / "stream_consumer_main.py")]

    print("=== zhongji-dev ===")
    print("API    → api.main:run (:8000)")
    print("Worker → stream_consumer_main (Stream 归档 + TTL 定时任务)")
    print("Ctrl+C 结束两个进程。\n")

    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen(api_cmd, cwd=str(ROOT), env=env))
        time.sleep(0.8)
        procs.append(subprocess.Popen(worker_cmd, cwd=str(ROOT), env=env))

        while True:
            for p in procs:
                code = p.poll()
                if code is not None:
                    print(f"\n[dev] process exited pid={p.pid} code={code}")
                    raise SystemExit(code or 0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[dev] interrupted, stopping…")
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        deadline = time.time() + 8
        for p in procs:
            remain = max(0.1, deadline - time.time())
            try:
                p.wait(timeout=remain)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("[dev] stopped.")


if __name__ == "__main__":
    main()
