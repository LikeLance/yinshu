"""银枢一键启动：Mock 核心系统(8010) + 后端(8000) + 自动打开浏览器。

用法：
    python start.py            # 演示模式（单进程、确定性、无限流）
    python start.py --no-open  # 不自动开浏览器（服务器/CI 用）
    python start.py --reset    # 先重建数据库再启动
    python start.py --prod     # 生产模式（多 worker、JSON 日志、限流开启）
    python start.py --workers 4   # 显式指定 worker 数
环境变量可从同目录 .env 读取（已存在的系统变量优先）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释，不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


load_dotenv(ROOT / ".env")

BANK_PORT = int(os.getenv("YINSHU_BANK_PORT", "8010"))
APP_PORT = int(os.getenv("YINSHU_PORT", "8000"))
BIND_HOST = os.getenv("YINSHU_HOST", "127.0.0.1")
BANK_HOST = "127.0.0.1" if BIND_HOST in ("0.0.0.0", "::") else BIND_HOST


def c(code: str) -> str:
    return f"\033[{code}m" if os.name != "nt" or os.getenv("WT_SESSION") else ""


def say(msg: str) -> None:
    print(f"[yinshu] {msg}", flush=True)


def port_busy(p: int) -> bool:
    host = "127.0.0.1"
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, p)) == 0


def wait_http(url: str, timeout: float = 30.0) -> bool:
    # 显式不走系统代理：本机回环地址一旦被代理接管，健康检查会一直失败
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with opener.open(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.35)
    return False


def main() -> int:
    reset = "--reset" in sys.argv
    no_open = "--no-open" in sys.argv
    prod = "--prod" in sys.argv
    workers = 0
    for i, a in enumerate(sys.argv):
        if a == "--workers" and i + 1 < len(sys.argv):
            workers = int(sys.argv[i + 1])
        elif a.startswith("--workers="):
            workers = int(a.split("=", 1)[1])

    if prod:
        os.environ.setdefault("YINSHU_ENV", "prod")
        workers = workers or int(os.getenv("YINSHU_WORKERS", "4"))
        no_open = True

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        import fastapi  # noqa: F401
        import httpx    # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        say("依赖缺失，请先执行：pip install -r requirements.txt")
        return 1

    from backend.core.config import BANK_DB

    if reset or not BANK_DB.exists():
        say("生成模拟银行数据（12 个月流水 / 通讯录 / 理财货架）...")
        subprocess.run([sys.executable, "-m", "backend.mock_bank.seed", "--reset"],
                       cwd=ROOT, env=env, check=True)

    procs: list[subprocess.Popen] = []

    def spawn(args: list[str], name: str) -> subprocess.Popen:
        p = subprocess.Popen(args, cwd=ROOT, env=env)
        procs.append(p)
        say(f"{name} 已启动 pid={p.pid}")
        return p

    if port_busy(BANK_PORT):
        say(f"端口 {BANK_PORT} 已被占用，复用现有 Mock 核心系统")
    else:
        spawn([sys.executable, "-m", "uvicorn", "backend.mock_bank.api:app",
               "--host", BANK_HOST, "--port", str(BANK_PORT), "--log-level", "warning"],
              "Mock 核心系统")

    if port_busy(APP_PORT):
        say(f"端口 {APP_PORT} 已被占用，可能已有一份银枢在跑，直接打开浏览器")
        if not no_open:
            webbrowser.open(f"http://127.0.0.1:{APP_PORT}")
        return 0

    app_args = [sys.executable, "-m", "uvicorn", "backend.main:app",
                "--host", BIND_HOST, "--port", str(APP_PORT)]
    if workers and workers > 1:
        # 多 worker：会话与审计都在 SQLite(WAL) 上，跨进程共享；限流计数为进程内
        app_args += ["--workers", str(workers)]
    else:
        app_args += ["--log-level", "warning"]
    spawn(app_args, f"银枢后端{'（生产模式，' + str(workers) + ' worker）' if workers > 1 else ''}")

    ok_bank = wait_http(f"http://127.0.0.1:{BANK_PORT}/v1/health")
    ok_app = wait_http(f"http://127.0.0.1:{APP_PORT}/api/health")
    if not (ok_bank and ok_app):
        say(f"启动失败：bank={ok_bank} app={ok_app}。请检查端口占用或依赖。")
        for p in procs:
            p.terminate()
        return 1

    url = f"http://{BIND_HOST if BIND_HOST not in ('0.0.0.0', '::') else '127.0.0.1'}:{APP_PORT}"
    say("=" * 56)
    say(f"银枢已就绪  →  {url}")
    say(f"Mock 核心系统 →  http://127.0.0.1:{BANK_PORT}/docs")
    say(f"接口文档      →  {url}/docs")
    say(f"运行环境      →  {os.getenv('YINSHU_ENV', 'demo')}"
        + (f"，{workers} worker" if workers > 1 else "，单进程"))
    say("Ctrl+C 停止全部服务")
    say("=" * 56)
    if not no_open:
        webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
            for p in list(procs):
                if p.poll() is not None:
                    say(f"子进程 {p.pid} 已退出（code={p.returncode}）")
                    procs.remove(p)
            if not procs:
                return 0
    except KeyboardInterrupt:
        say("正在停止...")
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        say("已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
