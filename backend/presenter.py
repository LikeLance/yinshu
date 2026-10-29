"""回复与卡片装配。

本模块只负责将结构化结果转换为用户可读文本和卡片数据，不执行权限判断。
所有数字都取自执行结果，模板里的字面数字会被 guard 记为白名单。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from backend.planner.contracts import Plan
from backend.planner.executor import Outcome
from backend.security import guard
from backend.security.policy import Decision

RULE_LABEL = {
    "NIGHT_BURST": "凌晨连刷",
    "DUPLICATE": "重复扣款",
    "STAT_OUTLIER": "大额离群",
    "ROUND_LARGE": "整数大额",
    "REMOTE_CITY": "异地消费",
}


def _f(x) -> str:
    try:
        return f"{Decimal(str(x)):.2f}"
    except Exception:
        return str(x)


def _rows(pairs: list[tuple[str, Any]]) -> list[list[str]]:
    return [[k, str(v)] for k, v in pairs if v not in (None, "")]


def plan_card(plan: Plan, decision: Decision | None) -> dict:
    return {
        "type": "plan",
        "title": "执行计划（DAG）",
        "steps": [{"id": s.id, "tool": s.tool, "label": s.label or s.tool, "gated": s.is_gated}
                  for s in plan.steps],
        "tier": decision.tier if decision else "-",
        "risk": decision.risk if decision else "-",
    }


def build(intent: str, plan: Plan, out: Outcome, decision: Decision | None,
          slots: dict) -> tuple[str, dict | None, set[str]]:
    r = out.results
    extra: set[str] = set()
    card: dict | None = None
    text = ""

    # ---------------- 转账 ----------------
    if intent == "transfer.create" and "s3" in r:
        t = r["s3"]
        memo = t.get("memo") or ""
        tpl = "已向 {payee}（尾号 {last4}）转账 {amount} 元{memo_part}，可用余额 {bal} 元。"
        text, st1 = guard.render(tpl, {
            "payee": t.get("payee_name", ""), "last4": t.get("payee_last4", ""),
            "amount": t.get("amount", ""),
            "memo_part": f"，备注「{memo}」" if memo else "",
            "bal": t.get("balance_after", ""),
        })
        extra |= st1 | {t.get("payee_last4", ""), t.get("payee_name", "")} - {""}
        card = {"type": "receipt", "title": "转账成功", "tone": "ok",
                "rows": _rows([("收款人", f"{t.get('payee_name')}（尾号 {t.get('payee_last4')}）"),
                               ("金额", f"{t.get('amount')} 元"),
                               ("备注", memo or "—"),
                               ("时间", t.get("created_at")),
                               ("凭据", f"TX{t.get('transfer_id')}")]),
                "note": f"可用余额 {t.get('balance_after')} 元"}

    elif intent == "transfer.schedule" and "s3" in r:
        t = r["s3"]
        rec = {"monthly": "每月", "weekly": "每周", "daily": "每天"}
        rec_txt = ""
        recurrence = t.get("recurrence") or ""
        if ":" in recurrence:
            kind, val = recurrence.split(":", 1)
            rec_txt = f"{rec.get(kind, '')}{val}{'号' if kind == 'monthly' else ''}"
        text, st1 = guard.render(
            "已设置定时转账：{rec}向 {payee} 转 {amount} 元，首次执行 {first}。",
            {"rec": rec_txt, "payee": t.get("payee_name", ""), "amount": t.get("amount", ""),
             "first": t.get("first_run_at", "")})
        extra |= st1
        card = {"type": "receipt", "title": "定时转账已生效", "tone": "ok",
                "rows": _rows([("收款人", t.get("payee_name")), ("金额", f"{t.get('amount')} 元"),
                               ("周期", rec_txt or "单次"), ("首次执行", t.get("first_run_at")),
                               ("凭据", f"SC{t.get('transfer_id')}")]),
                "note": "到期前可随时修改或撤销"}

    elif intent == "transfer.collect" and "s1" in r:
        t = r["s1"]
        text, st1 = guard.render(
            "AA 收款单已生成：总价 {total} 元、{people} 人，每人 {per} 元，差额已自动摊入最后一笔。",
            {"total": t.get("total", ""), "people": t.get("people", ""), "per": t.get("per_person", "")})
        extra |= st1
        card = {"type": "receipt", "title": "AA 收款已发起", "tone": "ok",
                "rows": _rows([("总额", f"{t.get('total')} 元"), ("人数", t.get("people")),
                               ("每人", f"{t.get('per_person')} 元"),
                               ("收款单号", t.get("request_id"))]),
                "note": "已向其余成员推送收款请求"}

    # ---------------- 余额 ----------------
    elif intent == "balance.query" and "s1" in r:
        accs = r["s1"].get("accounts") or []
        items = [{"kind": a["kind"], "last4": a["last4"], "balance": a["balance"]} for a in accs]
        total = sum((Decimal(a["balance"]) for a in accs), Decimal("0"))
        first = items[0] if items else {"balance": "0.00", "last4": ""}
        text, st1 = guard.render(
            "您的{kind}账户尾号 {last4} 余额 {bal} 元，全部账户合计 {total} 元。",
            {"kind": first["kind"], "last4": first["last4"], "bal": first["balance"],
             "total": _f(total)})
        extra |= st1 | {a["last4"] for a in items} | {_f(total)}
        card = {"type": "balance", "title": "账户余额",
                "rows": [[f"{a['kind']} 尾号 {a['last4']}", f"{a['balance']} 元"] for a in items],
                "note": f"合计 {_f(total)} 元"}

    # ---------------- 账单 ----------------
    elif intent == "bill.summary" and "s1" in r:
        t = r["s1"]
        cats = t.get("categories") or []
        top = cats[0] if cats else None
        mom = t.get("mom_pct")
        mom_txt = ""
        if mom is not None:
            mom_txt = f"，较上期{'上升' if mom >= 0 else '下降'} {abs(mom)}%"
        if top:
            text, st1 = guard.render(
                "{label}共支出 {total} 元、{count} 笔，最大头是{cat}（{amt} 元，占 {ratio}%）{mom}。",
                {"label": (plan.slots.get("period") or {}).get("label", "本期"),
                 "total": t.get("total_out", ""), "count": t.get("count", ""),
                 "cat": top["category"], "amt": top["amount"], "ratio": top["ratio"], "mom": mom_txt})
        else:
            text, st1 = guard.render("{label}没有支出记录。",
                                     {"label": (plan.slots.get("period") or {}).get("label", "本期")})
        extra |= st1
        card = {"type": "bill", "title": "账单结构分析",
                "range": t.get("range", ""),
                "total": t.get("total_out"), "count": t.get("count"), "mom": mom,
                "categories": cats[:6],
                "top": t.get("top_merchants") or []}

    elif intent == "bill.anomaly":
        a = (r.get("s1") or {})
        s = (r.get("s2") or {})
        items = a.get("anomalies") or []
        if items:
            from collections import Counter
            c = Counter(RULE_LABEL.get(x["rule"], x["rule"]) for x in items)
            main_rule = c.most_common(1)[0][0]
            text, st1 = guard.render(
                "这段流水中发现 {n} 笔可疑交易，主要类型为「{rule}」。结果已按时间排序，并附有判定原因。",
                {"n": len(items), "rule": main_rule})
        else:
            text, st1 = guard.render("这段流水没有发现明显异常。", {})
        extra |= st1
        card = {"type": "anomaly", "title": "异常交易核查", "tone": "warn",
                "count": len(items),
                "items": [{"ts": x["ts"], "merchant": x["merchant"], "amount": x["amount"],
                           "category": x["category"], "reason": x["reason"],
                           "rule": RULE_LABEL.get(x["rule"], x["rule"])} for x in items[:8]],
                "summary": s}

    # ---------------- 理财 ----------------
    elif intent == "wealth.compare" and "s1" in r:
        ps = (r["s1"].get("products") or [])[:6]
        ps.sort(key=lambda p: -float(p["annual_rate"]))
        if ps:
            text, st1 = guard.render(
                "按您当前的风险承受能力筛出 {n} 支产品，业绩比较基准从 {lo}% 到 {hi}%。我挑了 3 支做横向对比，点开看起购和赎回规则。",
                {"n": len(r["s1"].get("products") or []), "lo": ps[-1]["annual_rate"], "hi": ps[0]["annual_rate"]})
        else:
            text, st1 = guard.render("当前没有符合您风险等级的产品。", {})
        extra |= st1
        card = {"type": "wealth", "title": "理财对比", "products": ps[:3],
                "all_count": len(r["s1"].get("products") or [])}

    elif intent == "wealth.subscribe" and "s2" in r:
        t = r["s2"]
        text, st1 = guard.render(
            "已申购「{name}」{amount} 元。{note}",
            {"name": t.get("product", ""), "amount": t.get("amount", ""),
             "note": t.get("confirm_note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "申购已提交", "tone": "ok",
                "rows": _rows([("产品", t.get("product")), ("金额", f"{t.get('amount')} 元"),
                               ("业绩基准", f"{t.get('annual_rate')}%"),
                               ("确认方式", t.get("confirm_note"))]),
                "note": t.get("redeem_rule")}

    elif intent == "wealth.redeem" and "s2" in r:
        t = r["s2"]
        text, st1 = guard.render("已提交「{name}」赎回 {amount} 元，{note}。",
                                 {"name": t.get("product", ""), "amount": t.get("amount", ""),
                                  "note": t.get("arrive_note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "赎回申请已提交", "tone": "ok",
                "rows": _rows([("产品", t.get("product")), ("金额", f"{t.get('amount')} 元"),
                               ("到账", t.get("arrive_note")), ("赎回规则", t.get("redeem_rule"))])}

    # ---------------- 卡片 ----------------
    elif intent == "card.limit" and "s2" in r:
        t = r["s2"]
        picked = (r.get("s1") or {}).get("picked") or {}
        last4 = picked.get("last4", "")
        if last4:
            text, st1 = guard.render("尾号 {last4} 的日交易限额已调整为 {limit} 元，{note}。",
                                     {"last4": last4, "limit": t.get("daily_limit", ""),
                                      "note": t.get("note", "")})
        else:
            text, st1 = guard.render("日交易限额已调整为 {limit} 元，{note}。",
                                     {"limit": t.get("daily_limit", ""), "note": t.get("note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "限额已调整", "tone": "ok",
                "rows": _rows([("卡片", f"尾号 {last4}" if last4 else ""),
                               ("新日限额", f"{t.get('daily_limit')} 元"),
                               ("状态", t.get("status")), ("说明", t.get("note"))])}

    elif intent == "card.freeze" and "s2" in r:
        t = r["s2"]
        picked = (r.get("s1") or {}).get("picked") or {}
        last4 = picked.get("last4", "")
        act = "冻结" if t.get("frozen") else "解冻"
        if last4:
            text, st1 = guard.render("尾号 {last4} 的卡片已{act}，{note}。",
                                     {"last4": last4, "act": act, "note": t.get("note", "")})
        else:
            text, st1 = guard.render("卡片已{act}，{note}。", {"act": act, "note": t.get("note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "卡片状态已变更",
                "tone": "warn" if t.get("frozen") else "ok",
                "rows": _rows([("卡片", f"尾号 {last4}" if last4 else ""),
                               ("当前状态", "已冻结" if t.get("frozen") else "正常"),
                               ("说明", t.get("note"))])}

    elif intent == "card.loss" and "s2" in r:
        t = r["s2"]
        text, st1 = guard.render("卡片已挂失，{note}。", {"note": t.get("note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "已挂失（不可逆）", "tone": "danger",
                "rows": _rows([("状态", "已生效"), ("工单", t.get("ticket")), ("说明", t.get("note"))])}

    elif intent == "card.apply" and "s1" in r:
        t = r["s1"]
        text, st1 = guard.render("办卡申请已提交，{note}。", {"note": t.get("note", "")})
        extra |= st1
        card = {"type": "receipt", "title": "申请已提交", "tone": "ok",
                "rows": _rows([("卡种", t.get("card_type")), ("申请号", t.get("apply_id")),
                               ("状态", t.get("note"))])}

    elif intent == "card.list" and "s1" in r:
        cards = r["s1"].get("cards") or []
        st_map = {"active": "正常", "frozen": "已冻结", "lost": "已挂失"}
        want = str((slots.get("card_last4") or "")).strip()
        hit = next((c for c in cards if str(c["last4"]) == want), None) if want else None
        if hit:
            # 用户点名问某张卡，就别拿另一张去回答
            text, st1 = guard.render(
                "尾号 {last4}（{brand}）当前状态：{st}，日交易限额 {lim} 元。",
                {"last4": hit["last4"], "brand": hit["brand"],
                 "st": st_map.get(hit["status"], hit["status"]), "lim": hit["daily_limit"]})
        elif want and cards:
            text, st1 = guard.render("您名下没有尾号 {w} 的卡，下面是现有的卡。", {"w": want})
        elif cards:
            c0 = cards[0]
            text, st1 = guard.render(
                "您名下有 {n} 张卡，尾号 {last4} 的日交易限额为 {lim} 元。",
                {"n": len(cards), "last4": c0["last4"], "lim": c0["daily_limit"]})
        else:
            text, st1 = guard.render("您名下暂时没有可用的卡片。", {})
        extra |= st1 | {c["last4"] for c in cards}
        card = {"type": "cards", "title": "我的卡片",
                "items": [{"last4": c["last4"], "brand": c["brand"], "limit": c["daily_limit"],
                           "status": st_map.get(c["status"], c["status"])}
                          for c in cards]}

    # ---------------- 订阅 ----------------
    elif intent == "sub.list" and "s1" in r:
        t = r["s1"]
        items = t.get("items") or []
        note = next((i["note"] for i in items if i.get("note")), "")
        text, st1 = guard.render(
            "您有 {n} 项自动续费，年化成本 {total} 元。{note}",
            {"n": t.get("count", 0), "total": t.get("annual_total", ""),
             "note": f"其中「{items[0]['merchant']}」{items[0]['note']}。" if items and items[0].get("note") else
                     (f"（{note}）" if note else "")})
        extra |= st1
        card = {"type": "subs", "title": "订阅代扣清单", "annual_total": t.get("annual_total"),
                "items": [{"id": i["id"], "merchant": i["merchant"], "amount": i["amount"],
                           "cycle": i["cycle"], "annual": i["annual_cost"], "note": i.get("note")}
                          for i in items]}

    elif intent == "sub.cancel" and "s2" in r:
        t = r["s2"]
        text, st1 = guard.render("「{m}」已取消，{eff}。",
                                 {"m": t.get("merchant", ""), "eff": t.get("effective", "")})
        extra |= st1
        card = {"type": "receipt", "title": "订阅已取消", "tone": "ok",
                "rows": _rows([("商户", t.get("merchant")), ("状态", "已取消"),
                               ("生效说明", t.get("effective"))])}

    # ---------------- 跨场景联动 ----------------
    elif intent == "cross.birthday" and "s2" in r:
        gifts = (r["s2"].get("gifts") or [])
        hold = r.get("s1") or {}
        text, st1 = guard.render(
            "已锁定 {amt} 元专项资金（订单 {hid}），并生成 {n} 个方案。选择后将按所选方案下单；未选择前不会下单。",
            {"amt": hold.get("amount", ""), "hid": hold.get("hold_id", ""), "n": len(gifts)})
        extra |= st1
        card = {"type": "choose", "title": "选一个礼物方案", "kind": "gift",
                "options": [{"id": g["gift_id"], "label": g["name"],
                             "sub": f"{g['amount']} 元 · {g['vendor']}"} for g in gifts],
                "note": f"专项资金 {hold.get('amount')} 元已锁定，确认方案后扣款"}

    elif intent == "gift.order" and "s1" in r:
        t = r["s1"]
        text, st1 = guard.render("已下单「{n}」，{amt} 元，由 {v} 配送，订单号 {oid}。",
                                 {"n": t.get("name", ""), "amt": t.get("amount", ""),
                                  "v": t.get("vendor", ""), "oid": t.get("order_id", "")})
        extra |= st1
        card = {"type": "receipt", "title": "礼物已下单", "tone": "ok",
                "rows": _rows([("商品", t.get("name")), ("金额", f"{t.get('amount')} 元"),
                               ("商家", t.get("vendor")), ("订单号", t.get("order_id"))])}

    if not text:
        text = "这件事我还不会办，换个说法或换成转账、账单、理财、卡片、订阅类的事儿试试。"
    return text, card, extra


def confirm_rows(plan: Plan, slots: dict, out: executor.Outcome) -> list[list[str]]:
    rows: list[list[str]] = []
    payee = ((out.results.get("s1") or {}).get("payee") or {})
    if payee:
        rows.append(["收款人", f"{payee.get('name')}（{payee.get('relation')}，尾号 {payee.get('account_last4')}）"])
    if slots.get("amount") is not None:
        rows.append(["金额", f"{Decimal(str(slots['amount'])):.2f} 元"])
    if slots.get("budget") is not None:
        rows.append(["预算", f"{Decimal(str(slots['budget'])):.2f} 元"])
    if slots.get("gift_name"):
        rows.append(["礼物", slots["gift_name"]])
        if slots.get("gift_amount"):
            rows.append(["金额", f"{Decimal(str(slots['gift_amount'])):.2f} 元"])
    w = slots.get("when") or {}
    if w.get("label"):
        rows.append(["时间", w["label"]])
    if slots.get("memo"):
        rows.append(["备注", slots["memo"]])
    picked = (out.results.get("s1") or {}).get("picked")
    if isinstance(picked, dict):
        if picked.get("name"):
            rows.append(["产品" if "risk" in picked else "标的", f"{picked['name']}"])
        if picked.get("risk"):
            rows.append(["风险等级", f"{picked['risk']} / 业绩基准 {picked.get('annual_rate')}%"])
        if picked.get("merchant"):
            rows.append(["订阅", f"{picked['merchant']}（{picked['amount']} 元/{picked['cycle']}）"])
        if picked.get("last4"):
            rows.append(["卡片", f"尾号 {picked['last4']}"])
        if picked.get("amount") and "cycle" not in picked:
            rows.append(["持仓金额", f"{picked['amount']} 元"])
    if not rows:
        rows.append(["动作", plan.steps[-1].label or plan.steps[-1].tool])
    return rows
