"""结构化日志。

演示时人看，上线时机读——所以两种格式都支持，由 YINSHU_LOG_JSON 控制。
日志里一律不带完整卡号/账号，只留后四位（见 `mask()`）。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from backend.core import config

_RESERVED = {"name", "msg", "args", "levelname", "levelno", "pathname", "filename",
             "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
             "created", "msecs", "relativeCreated", "thread", "threadName",
             "processName", "process", "taskName", "message", "asctime"}


def mask(value: Any, keep: int = 4) -> str:
    """把账号/卡号/手机号打码成 ****1234，审计与日志都走它。"""
    s = str(value or "")
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) <= keep:
        return s
    return f"****{digits[-keep:]}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                try:
                    json.dumps(v, ensure_ascii=False)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = str(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-5s %(name)-22s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = {k: v for k, v in record.__dict__.items()
                 if k not in _RESERVED and not k.startswith("_")}
        if extra:
            tail = " ".join(f"{k}={v}" for k, v in extra.items())
            return f"{base}  {tail}"
        return base


def setup() -> None:
    root = logging.getLogger("yinshu")
    if root.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter() if config.LOG_JSON else TextFormatter())
    root.addHandler(h)
    root.setLevel(config.LOG_LEVEL)
    root.propagate = False


class _Adapter:
    """让调用处能写 log.info("msg", trace_id=..., ms=12) 这种结构化字段。

    标准库 logging 不接受任意关键字，所以这里统一转成 extra={...}。
    """

    __slots__ = ("_l",)

    def __init__(self, logger: logging.Logger) -> None:
        self._l = logger

    def _log(self, level: int, msg: str, /, **kw) -> None:
        exc_info = kw.pop("exc_info", None)
        extra = {k: v for k, v in kw.items() if k not in _RESERVED}
        self._l.log(level, msg, extra=extra, exc_info=exc_info)

    def debug(self, msg: str, /, **kw) -> None:
        self._log(logging.DEBUG, msg, **kw)

    def info(self, msg: str, /, **kw) -> None:
        self._log(logging.INFO, msg, **kw)

    def warning(self, msg: str, /, **kw) -> None:
        self._log(logging.WARNING, msg, **kw)

    def error(self, msg: str, /, **kw) -> None:
        self._log(logging.ERROR, msg, **kw)

    def exception(self, msg: str, /, **kw) -> None:
        kw.setdefault("exc_info", True)
        self._log(logging.ERROR, msg, **kw)


def get(name: str) -> _Adapter:
    setup()
    return _Adapter(logging.getLogger(f"yinshu.{name}"))
