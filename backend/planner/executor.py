"""执行器：拓扑执行 DAG、解析 $ref、失败补偿。

两阶段执行是安全设计的一部分：
- preview：只跑"不改变世界"的步骤（查余额、搜收款人、筛产品），拿到事实；
- commit：权限闸门放行后才跑受控步骤（转钱、申购、挂失、取消订阅）。
所以"确认"之前，不会有任何资金变动。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backend.core.errors import Ambiguous, YinshuError
from backend.planner.contracts import Plan, Step
from backend.security import guard
from backend.tools import gateway

def _hold_args(out: "Outcome", plan: "Plan") -> dict | None:
    """找出本计划里锁定资金的 hold，用于失败时释放。

    生日这类跨场景 DAG 是「先锁预算，再下单」；下单失败如果不释放，
    那笔钱就永久挂在 holds 表里——原实现只有 hold.create 一个补偿器，
    gift.order 失败后锁定的预算再也没人解。

    hold_id 可能在本次结果里，也可能是上一阶段（选礼物前）锁好后带到 slots 的。
    """
    for r in out.results.values():
        if isinstance(r, dict) and r.get("hold_id"):
            return {"hold_id": r["hold_id"]}
    hid = (plan.slots or {}).get("hold_id")
    return {"hold_id": hid} if hid else None


# 补偿器签名：(outcome, step_id, plan) -> 调用参数 | None
COMPENSATORS: dict[str, tuple[str, Any]] = {
    "hold.create": ("hold.release",
                    lambda out, sid, plan: {"hold_id": out.results[sid]["hold_id"]}),
    "gift.order": ("hold.release", lambda out, sid, plan: _hold_args(out, plan)),
}


@dataclass
class Outcome:
    results: dict[str, dict] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def resolve(value: Any, slots: dict, results: dict) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        p = value[1:]
        if p.startswith("slots."):
            return slots.get(p[6:])
        sid, _, key = p.partition(".")
        return (results.get(sid) or {}).get(key)
    if isinstance(value, dict):
        return {k: resolve(v, slots, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, slots, results) for v in value]
    return value


def _check_ambiguous(name: str, data: dict) -> None:
    if not data.get("ambiguous"):
        return
    opts = data.get("options") or []
    kind = {"payee.search": "payee", "sub.list": "sub", "wealth.products": "product",
            "wealth.holdings": "holding", "card.list": "card"}.get(name, "item")
    raise Ambiguous(kind, opts)


def _compensate(out: Outcome, plan: Plan, trace_id: str, principal: Any = None) -> None:
    for sid in reversed(out.executed):
        step = next((s for s in plan.steps if s.id == sid), None)
        if not step or step.on_fail != "compensate":
            continue
        pair = COMPENSATORS.get(step.tool)
        if not pair:
            continue
        tool, mapper = pair
        try:
            args = mapper(out, sid, plan)
            if not args:
                continue
            gateway.call(tool, args, trace_id, f"{sid}-comp", principal=principal)
            out.compensated.append(sid)
        except YinshuError as e:
            out.errors.append(f"补偿失败 {tool}: {e.code}")


def run(plan: Plan, phase: str = "preview", trace_id: str = "", state: Outcome | None = None,
        ignore_ambiguous: bool = False, principal: Any = None,
        idem_seed: str | None = None) -> Outcome:
    out = state or Outcome()
    if phase == "preview":
        for step in plan.steps:
            if step.is_gated:
                out.pending = [s.id for s in plan.steps if s.id not in out.results]
                return out
            _exec(plan, step, out, trace_id, ignore_ambiguous, principal, idem_seed)
        out.pending = []
        return out

    for step in plan.steps:
        if step.id in out.results:
            continue
        try:
            _exec(plan, step, out, trace_id, ignore_ambiguous, principal, idem_seed)
        except YinshuError as e:
            out.errors.append(f"{step.id}:{getattr(e, 'code', 'ERR')}")
            if step.on_fail == "abort":
                _compensate(out, plan, trace_id, principal)
                raise
    out.pending = [s.id for s in plan.steps if s.id not in out.results and s.is_gated]
    return out


def _exec(plan: Plan, step: Step, out: Outcome, trace_id: str,
          ignore_ambiguous: bool = False, principal: Any = None,
          idem_seed: str | None = None) -> None:
    args = resolve(step.args, plan.slots, out.results)
    args = {k: v for k, v in args.items() if v is not None}
    data = gateway.call(step.tool, args, trace_id, step.id,
                        principal=principal, idem_seed=idem_seed)
    out.results[step.id] = data
    out.executed.append(step.id)
    if not ignore_ambiguous:
        _check_ambiguous(step.tool, data)


# ---------------- 事实集：幻觉防护的比对基准 ----------------

# 取数规则必须与 guard.check() 一致：如果这里把「2026-10-28 22:43:06」拆成六个数字
# 塞进白名单，而校验那边也拆，那"编造一个 22"就永远能蒙中。统一走 guard.numbers_in()。
NUM_RE = guard.NUM_RE


def _walk(obj: Any, acc: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("_meta",):
                continue
            _walk(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, acc)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float, Decimal)):
        acc.add(f"{obj}")
        try:
            acc.add(f"{Decimal(str(obj)):.2f}")
        except Exception:
            pass
    elif isinstance(obj, str):
        acc.update(guard.numbers_in(obj))


def facts(slots: dict, results: dict, extra: set[str] | None = None) -> set[str]:
    acc: set[str] = set(extra or set())
    _walk(slots, acc)
    _walk(results, acc)
    return acc
