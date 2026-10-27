"""统一时钟。

为什么要单独一层：权限引擎的「凌晨 0-6 点操作」加分项直接读 wall clock，
导致同一句「给我妈转500」在白天是 AUTO、在凌晨变成 CONFIRM——测试与演示
都会随运行时刻漂移，回归结果不可复现。

生产路径仍然读真实时间，测试用 `freeze()` 把时间钉住。
"""
from __future__ import annotations

from datetime import datetime

_now = datetime.now


def now() -> datetime:
    return _now()


def today_yyyymmdd() -> str:
    return _now().strftime("%Y%m%d")


def freeze(dt: datetime) -> None:
    """测试专用：把时钟钉在某一刻。"""
    global _now
    _now = lambda: dt  # noqa: E731


def unfreeze() -> None:
    global _now
    _now = datetime.now


def frozen() -> bool:
    return _now is not datetime.now
