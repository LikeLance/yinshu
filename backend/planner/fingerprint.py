"""计划指纹：把"用户看到的那张确认卡"钉死成一段哈希。

为什么不能只哈希 `Plan`：Plan 里的参数是 `$s1.chosen` 这类引用，
两次 preview 可能解析到**不同的收款人**（同名、候选顺序变化），
而 Plan 本身一个字都没变。指纹必须覆盖**解析后的真实参数**。

有了指纹，"用户点确认时执行的那一笔"与"他看到的那一笔"就有了可判定的一致性；
确认阶段重算一次指纹，不一致就要求重新确认——这是防 TOCTOU 的第一道。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.planner.contracts import Plan
from backend.planner.executor import resolve


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def of(plan: Plan, slots: dict, results: dict) -> str:
    """对"会动钱/会改状态"的步骤生成指纹；只读步骤不参与（它们不影响结果）。"""
    gated = []
    for st in plan.steps:
        if not st.is_gated:
            continue
        gated.append({
            "id": st.id,
            "tool": st.tool,
            "on_fail": st.on_fail,
            "args": resolve(st.args, slots, results),
        })
    doc = {
        "v": 2,
        "intent": plan.intent,
        "steps": gated,
        "when": slots.get("when") or {},
        "memo": slots.get("memo") or "",
    }
    return hashlib.sha256(_canon(doc).encode("utf-8")).hexdigest()
