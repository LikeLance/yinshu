"""幻觉防护：模型（或模板）产出的每一句话，数字都必须有出处。

机制：
1. 渲染时把模板里的**字面数字**记下来（这部分是人写的，天然可信）；
2. 运行时的数字只能来自 slots 与工具返回值（事实集）；
3. 逐句比对，出现"事实集之外"的数字即判违规 → 重写 → 仍违规则降级为纯模板回执。

规则引擎路径下违规率天然为 0；这个模块真正的作用是给模型路径兜底，
以及在评测里量化"拦截了多少次"。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _norm(n: str) -> str:
    try:
        return format(Decimal(n).normalize(), "f")
    except (InvalidOperation, ValueError):
        return n


def render(tpl: str, mapping: dict) -> tuple[str, set[str]]:
    """返回 (渲染结果, 模板自带数字集合)。"""
    static = {_norm(x) for x in NUM_RE.findall(tpl)}
    try:
        return tpl.format(**mapping), static
    except (KeyError, IndexError):
        return tpl, static


def check(text: str, allowed: set[str]) -> list[str]:
    """返回违规数字列表；空列表表示通过。"""
    allow = {_norm(a) for a in allowed}
    bad = []
    for n in NUM_RE.findall(text):
        if _norm(n) not in allow:
            bad.append(n)
    return bad


def guard_or_fallback(text: str, allowed: set[str], fallback: str) -> tuple[str, list[str]]:
    bad = check(text, allowed)
    return (fallback if bad else text), bad
