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
from backend.tools import gateway

COMPENSATORS: dict[str, tuple[str, Any]] = {
    "hold.create": ("hold.release", lambda r: {"hold_id": r["hold_id"]}),
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


def _compensate(out: Outcome, plan: Plan, trace_id: str) -> None:
    for sid in reversed(out.executed):
        step = next((s for s in plan.steps if s.id == sid), None)
        if not step or step.on_fail != "compensate":
            continue
        pair = COMPENSATORS.get(step.tool)
        if not pair:
            continue
        tool, mapper = pair
        try:
            gateway.call(tool, mapper(out.results[sid]), trace_id, f"{sid}-comp")
            out.compensated.append(sid)
        except YinshuError as e:
            out.errors.append(f"补偿失败 {tool}: {e.code}")


def run(plan: Plan, phase: str = "preview", trace_id: str = "", state: Outcome | None = None,
        ignore_ambiguous: bool = False) -> Outcome:
    out = state or Outcome()
    if phase == "preview":
        for step in plan.steps:
            if step.is_gated:
                out.pending = [s.id for s in plan.steps if s.id not in out.results]
                return out
            _exec(plan, step, out, trace_id, ignore_ambiguous)
        out.pending = []
        return out

    for step in plan.steps:
        if step.id in out.results:
            continue
        try:
            _exec(plan, step, out, trace_id, ignore_ambiguous)
        except YinshuError as e:
            out.errors.append(f"{step.id}:{getattr(e, 'code', 'ERR')}")
            if step.on_fail == "abort":
                _compensate(out, plan, trace_id)
                raise
    out.pending = [s.id for s in plan.steps if s.id not in out.results and s.is_gated]
    return out


def _exec(plan: Plan, step: Step, out: Outcome, trace_id: str, ignore_ambiguous: bool = False) -> None:
    args = resolve(step.args, plan.slots, out.results)
    args = {k: v for k, v in args.items() if v is not None}
    data = gateway.call(step.tool, args, trace_id, step.id)
    out.results[step.id] = data
    out.executed.append(step.id)
    if not ignore_ambiguous:
        _check_ambiguous(step.tool, data)


# ---------------- 事实集：幻觉防护的比对基准 ----------------

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


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
        acc.update(NUM_RE.findall(obj))


def facts(slots: dict, results: dict, extra: set[str] | None = None) -> set[str]:
    acc: set[str] = set(extra or set())
    _walk(slots, acc)
    _walk(results, acc)
    return acc
