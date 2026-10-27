"""候选选择与槽位补全：接住"还差一点信息"的那一轮。"""
from __future__ import annotations

import re

from backend.nlu.slots import parse_amount, parse_payee, parse_people, parse_when
from backend.session import fsm


def choose_response(kind: str, options: list[dict], intent: str) -> dict:
    if kind == "payee":
        opts = [{"id": str(o["id"]), "label": f"{o['name']}（{o.get('relation')}）",
                 "sub": f"{o.get('bank')} 尾号 {o.get('account_last4')}"} for o in options]
        title, reply = "找到多位可能的收款人", "通讯录里有几位对得上，选一位我再往下办。"
    elif kind == "gift":
        opts = [{"id": o["gift_id"], "label": o["name"], "sub": f"{o['amount']} 元"} for o in options]
        title, reply = "选一个方案", "选一个我来下单。"
    elif kind == "card":
        st_map = {"active": "正常", "frozen": "已冻结", "lost": "已挂失"}
        opts = [{"id": str(o["last4"]), "label": f"尾号 {o['last4']}（{o['brand']}）",
                 "sub": f"{st_map.get(o['status'], o['status'])} · 日限额 {o['daily_limit']} 元"}
                for o in options]
        title = "要操作哪张卡"
        reply = f"名下有 {len(options)} 张卡，先确认是哪一张——这一步不可逆，我不替你猜。"
    else:
        opts = [{"id": str(o.get("id", o.get("gift_id", ""))),
                 "label": o.get("name") or o.get("merchant") or str(o.get("id")),
                 "sub": str(o.get("amount", ""))} for o in options]
        title, reply = "找到多个匹配项", "选一个继续。"
    return {"reply": reply, "card": {"type": "choose", "title": title, "kind": kind, "options": opts},
            "debug": {"intent": intent, "ambiguous": kind}}


def pick_option(s: fsm.Session, text: str) -> dict | None:
    opts = s.stash.get("options") or []
    t = text.strip()
    idx = None
    for i, w in enumerate(["一", "二", "三", "四", "五"], start=1):
        if f"第{w}" in t or t == w:
            idx = i - 1
            break
    if idx is None:
        import re
        m = re.search(r"第?\s*([1-9])\s*(?:个|支|款|号)?", t)
        if m:
            idx = int(m.group(1)) - 1
    if idx is not None and 0 <= idx < len(opts):
        return opts[idx]
    for o in opts:
        label = o.get("name") or o.get("merchant") or ""
        if label and label in t:
            return o
        if o.get("relation") and o["relation"] in t:
            return o
        if o.get("last4") and str(o["last4"]) in t:      # 用户直接报尾号
            return o
    return opts[0] if len(opts) == 1 else None


def merge_clarify(s: fsm.Session, text: str) -> tuple[str, dict]:
    st = s.stash
    slots = dict(st.get("slots") or {})
    amt = parse_amount(text)
    if amt is not None:
        slots["amount"] = amt
    p = parse_payee(text)
    if not p:
        cand = text.strip().strip("给转付打 ")
        if 1 <= len(cand) <= 4 and all("\u4e00" <= c <= "\u9fa5" for c in cand):
            p = cand
    if p:
        slots["payee_raw"] = p
    w = parse_when(text)
    if w.get("explicit"):
        slots["when"] = w
    pp = parse_people(text)
    if pp:
        slots["people"] = pp
    if st.get("intent") == "wealth.subscribe" and not slots.get("product_keyword") and len(text) <= 12:
        slots["product_keyword"] = text.strip()
    if st.get("intent") == "sub.cancel" and not slots.get("merchant_keyword"):
        slots["merchant_keyword"] = text.strip()
    if st.get("intent") == "wealth.redeem" and not slots.get("product_keyword"):
        slots["product_keyword"] = text.strip()
    return st.get("intent") or "unknown", slots
