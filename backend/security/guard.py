"""幻觉防护：模型（或模板）产出的每一句话，数字都必须有出处。

机制：
1. 渲染时把模板里的**字面数字**记下来（这部分是人写的，天然可信）；
2. 运行时的数字只能来自 slots 与工具返回值（事实集）；
3. 逐句比对，出现"事实集之外"的数字即判违规 → 重写 → 仍违规则降级为纯模板回执。

规则引擎路径下违规率天然为 0；这个模块真正的作用是给模型路径兜底，
以及在评测里量化"拦截了多少次"。

三个能绕过去的缝，现在都堵上了：
- **中文数字**："已转出五百元"里没有阿拉伯数字，原来的 `\\d+` 一个也抓不到 ——
  接上大模型之后，把数字写成中文就是绕过幻觉拦截的后门；
- **日期/时间**：`2026-10-28 22:43:06` 会被拆成 2026/10/28/22/43/06 六个"事实"，
  反过来把白名单灌水，任何编造的两位数都可能蒙中；
- **取数与研判口径不一**：`executor.facts()` 与 `check()` 必须用同一套取数规则，
  否则白名单和校验各说各话（这里提供 `numbers_in()` 给两边共用）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from backend.core import cnnum

NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 日期 / 时间：先整体剥离，避免把年月日拆成一堆两位数灌进白名单
DATE_RE = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*[日号]?"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?")
TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")

# 中文数字：只认带量级单位的（十百千万亿）且至少两个字。
# "一次性""二手"这类没有量级单位，不算数字，不该被误判成违规。
# 字符集与换算都来自 backend/core/cnnum.py —— 与 NLU 的金额解析共用同一份实现。
CN_RUN_RE = re.compile(rf"[{cnnum.CHARS}]{{2,}}")


def strip_dates(s: str) -> str:
    return TIME_RE.sub(" ", DATE_RE.sub(" ", s or ""))


def _norm(n: str) -> str:
    try:
        return format(Decimal(n).normalize(), "f")
    except (InvalidOperation, ValueError):
        return n


def cn_to_number(run: str) -> int | None:
    """中文数字 → 整数。实现在 `core/cnnum.py`，与 NLU 金额解析共用一份。"""
    return cnnum.to_int(run)


def numbers_in(s: str) -> list[str]:
    """取一段文本里的所有数字字面量（阿拉伯 + 中文），取数与研判共用。"""
    s = strip_dates(s)
    out = NUM_RE.findall(s)
    for run in CN_RUN_RE.findall(s):
        v = cn_to_number(run)
        if v is not None:
            out.append(str(v))
    return out


def render(tpl: str, mapping: dict) -> tuple[str, set[str]]:
    """返回 (渲染结果, 模板自带数字集合)。"""
    static = {_norm(x) for x in numbers_in(tpl)}
    try:
        return tpl.format(**mapping), static
    except (KeyError, IndexError):
        return tpl, static


def check(text: str, allowed: set[str]) -> list[str]:
    """返回违规数字列表；空列表表示通过。"""
    allow = {_norm(a) for a in allowed}
    bad: list[str] = []
    for n in NUM_RE.findall(strip_dates(text)):
        if _norm(n) not in allow:
            bad.append(n)
    for run in CN_RUN_RE.findall(text or ""):
        v = cn_to_number(run)
        if v is None or _norm(str(v)) not in allow:
            bad.append(f"中文数字:{run}")
    return bad


def guard_or_fallback(text: str, allowed: set[str], fallback: str) -> tuple[str, list[str]]:
    bad = check(text, allowed)
    return (fallback if bad else text), bad
