"""数字来源校验。

机制：
1. 渲染时记录模板中的字面数字；
2. 运行时数字来自槽位和工具返回值；
3. 比对文本中的数字与事实集，出现额外数字时回退到模板文本并记录审计。

规则引擎路径不会生成未知数字，该校验主要用于未来接入模型生成文本的场景，
同时作为回归测试的一部分。当前覆盖范围是回复和部分卡片字段。

已处理的边界：
- 中文数字需要与阿拉伯数字使用同一套换算规则；
- 日期和时间先整体剥离，避免年月日被拆成多个独立数字；
- 事实集提取与校验共用 `numbers_in()`，防止两处规则不一致。
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
