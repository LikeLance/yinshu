"""统一时钟。

为什么要单独一层：权限引擎的「凌晨 0-6 点操作」加分项直接读 wall clock，
导致同一句「给我妈转500」在白天是 AUTO、在凌晨变成 CONFIRM——测试与演示
都会随运行时刻漂移，回归结果不可复现。

生产路径仍然读真实时间；测试用 `freeze()` 把时间钉住；
演示可以用 `YINSHU_FIXED_HOUR=14` 只把「小时」钉住（prod 下忽略），
这样 README 里那 13 场剧本的结果不会因为凌晨答辩而变档。
"""
from __future__ import annotations

import os
from datetime import datetime

_now = datetime.now

# 只钉小时，不动日期：'这个月/下周三' 这类相对时间仍按真实日期解析
_env_hour = os.getenv("YINSHU_FIXED_HOUR", "").strip()
_ENV = os.getenv("YINSHU_ENV", "demo").strip().lower()
_FIXED_HOUR: int | None = None
if _env_hour and _ENV != "prod":
    try:
        _FIXED_HOUR = int(_env_hour) % 24
    except ValueError:
        _FIXED_HOUR = None


def now() -> datetime:
    d = _now()
    return d.replace(hour=_FIXED_HOUR) if _FIXED_HOUR is not None else d


def today_yyyymmdd() -> str:
    return now().strftime("%Y%m%d")


def freeze(dt: datetime) -> None:
    """测试专用：把时钟钉在某一刻。"""
    global _now
    _now = lambda: dt  # noqa: E731


def unfreeze() -> None:
    global _now
    _now = datetime.now


def frozen() -> bool:
    return _now is not datetime.now
