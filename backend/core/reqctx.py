"""请求级上下文（ContextVar）。

为什么用 ContextVar 而不是把 principal 一路当参数传：
`_handle / _run_plan / _execute_pending / presenter / audit` 有二十多个调用点，
逐个加参数会把 diff 铺满整个 main.py，反而更容易改错。ContextVar 是"请求作用域"
的正确工具——Starlette 把同步端点丢进线程池时会复制当前 context，
函数内 set 的值不会泄漏到下一个请求。

注意：只放"这次请求的身份与幂等种子"，不放业务状态。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReqCtx:
    principal: Any = None
    trace_id: str = ""
    idem_seed: str = ""


_ctx: ContextVar[ReqCtx] = ContextVar("yinshu_req_ctx", default=ReqCtx())


def begin(principal: Any, trace_id: str, idem_seed: str = "") -> Token:
    return _ctx.set(ReqCtx(principal, trace_id, idem_seed))


def reset(token: Token) -> None:
    _ctx.reset(token)


def get() -> ReqCtx:
    return _ctx.get()


def principal() -> Any:
    return _ctx.get().principal


def idem_seed() -> str:
    return _ctx.get().idem_seed


def trace_id() -> str:
    return _ctx.get().trace_id
