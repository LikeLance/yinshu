"""请求内视图助手：JSON 化、通知卡、幻觉校验、工具输出扫描、统一响应体。

共同点：只做"把结构化数据翻译成对外形态"，不含业务判断，因此不依赖会话 /
规划 / 执行，任何层都可以安全复用。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from backend.planner import executor
from backend.security import audit, guard, injection


def jsonable(o: Any) -> Any:
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    return o


def notice(tone: str, title: str, lines: list[str], **extra) -> dict:
    return {"type": "notice", "tone": tone, "title": title, "lines": lines, **extra}


def card_text(card: dict | None) -> str:
    """抽出卡片上所有"用户会读到的文字"，用于幻觉校验。

    原实现只校验 reply，卡片完全不过闸——而卡片里的数字恰恰是用户最信的那部分
    （"已向 X 转账 8000 元" 写在卡片上比写在正文里更像事实）。
    """
    if not isinstance(card, dict):
        return ""
    parts: list[str] = []
    for k in ("title", "note"):
        v = card.get(k)
        if isinstance(v, str):
            parts.append(v)
    for row in card.get("rows") or []:
        parts.extend(str(x) for x in row)
    for line in card.get("lines") or []:
        parts.append(str(line))
    return " ".join(parts)


def guard_check(text_out: str, card: dict | None, allowed: set[str]) -> list[str]:
    return guard.check(text_out, allowed) + guard.check(card_text(card), allowed)


_UNTRUSTED_KEYS = ("memo", "reason", "merchant", "name", "title", "desc")


def scan_tool_output(out: executor.Outcome) -> injection.ScanResult:
    """把工具结果里"人写的文本"当不可信数据扫一遍（L2 提示隔离）。"""
    fields: dict[str, str] = {}
    for r in out.results.values():
        if not isinstance(r, dict):
            continue
        for k in _UNTRUSTED_KEYS:
            v = r.get(k)
            if isinstance(v, str) and v:
                fields[k] = v
        for sub in ("payee", "picked"):
            nested = r.get(sub)
            if isinstance(nested, dict):
                for k in ("name", "memo", "merchant"):
                    v = nested.get(k)
                    if isinstance(v, str) and v:
                        fields[f"{sub}_{k}"] = v
    return injection.scan_untrusted_data(fields)


# ---------------- 确认阶段：指纹 / 快照 / 漂移 ----------------


def first_of(results: dict, key: str) -> Any:
    """在任意步骤结果里找第一个出现的字段，避免把 's1'/'s2' 这类约定写死。"""
    for r in results.values():
        if isinstance(r, dict) and r.get(key) is not None:
            return r[key]
    return None


def build_response(s: fsm.Session, trace: str, reply: str, card: dict | None, debug: dict, t0: float) -> dict:
    chain = audit.append(trace, "response", {"reply": reply[:80]}, "ok")
    # 补齐三件套，保证任何返回路径的 debug 结构一致——集成方不用按分支猜字段
    d = {"guard_violations": [], "plan": [], "state": s.state, **debug}
    return jsonable({"session_id": s.sid, "trace_id": trace, "state": s.state,
                      "reply": reply, "card": card,
                      "debug": {**d, "audit": chain,
                                "latency_ms": int((time.time() - t0) * 1000)}})


# ---------------- 运维 / 调试接口 ----------------
