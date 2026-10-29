"""测试夹具：临时数据目录 + 进程内银行 + 借由 gateway 重定向。

关键点：先在导入 backend 之前改环境变量，否则 config 已经把路径定死了。
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="yinshu_test_"))

# pyproject 里 addopts 指定了 --basetemp=.tmp/pytest；`.tmp/` 不进仓库，
# 而 pytest 9 建 basetemp 时只做 mkdir(exist_ok=True)，父目录缺了就直接 FileNotFoundError。
# 在 conftest 导入（早于 basetemp 解析）时把父目录备好。
(Path(__file__).resolve().parents[1] / ".tmp").mkdir(parents=True, exist_ok=True)

os.environ["YINSHU_DATA_DIR"] = str(_TMP)
os.environ["YINSHU_ENV"] = "dev"
os.environ["YINSHU_SESSION_BACKEND"] = "sqlite"
os.environ["YINSHU_RATE_LIMIT_RPM"] = "0"
os.environ["YINSHU_LOG_JSON"] = "1"
os.environ["YINSHU_LOG_LEVEL"] = "ERROR"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bank_url():
    """把 Mock 核心系统跑在一个后台线程里，测试不依赖外部进程。"""
    import uvicorn
    from backend.mock_bank import seed as seed_mod
    from backend.mock_bank.api import app as bank_app

    seed_mod.seed()                       # 温度：12 个月流水、7 个收款人、6 项订阅
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(bank_app, host="127.0.0.1", port=port,
                                           log_level="error", access_log=False))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with opener.open(url + "/v1/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:                                  # pragma: no cover
        raise RuntimeError("Mock 核心系统没起来")

    yield url
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="session", autouse=True)
def _frozen_clock():
    """将权限引擎使用的时钟固定在白天。

    policy.json 包含“凌晨 0-6 点”加分的规则。如果不固定时间，同一句
    “给我妈转500”在凌晨会升为 CONFIRM，在白天则为 AUTO，测试结果会随运行时刻变化。
    """
    from datetime import datetime

    from backend.core import clock

    clock.freeze(datetime(2026, 10, 27, 12, 0, 0))
    yield
    clock.unfreeze()


@pytest.fixture(scope="session", autouse=True)
def _patch_bank(bank_url):
    from backend.tools import gateway

    old = gateway.BANK_BASE
    gateway.BANK_BASE = bank_url
    gateway.close()
    yield
    gateway.BANK_BASE = old
    gateway.close()


@pytest.fixture()
def clean_bank():
    """将银行数据恢复为种子状态。

    多条用例需要断言只新增了一笔转账，而 velocity_1h 是整套测试共享的银行状态。
    前序用例产生的资金操作会改变风险分，因此需要重置数据以避免顺序依赖。
    """
    from backend.mock_bank import seed as seed_mod

    # 只重灌数据、不删库文件——进程内的 Mock 核心还开着连接，删文件会 WinError 32
    seed_mod.seed()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as c:
        yield c
