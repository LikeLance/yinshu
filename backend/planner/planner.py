"""任务规划器：将意图和槽位转换为模板化 DAG。

采用模板而不是模型自由生成的原因：
- 银行场景的动作集合有限，模板可以覆盖已支持的业务；
- 计划结构稳定，便于测试和审计；
- 模型不参与选择工具，避免生成不存在的动作。

当前仓库不包含模型规划路径。
"""
from __future__ import annotations

from decimal import Decimal

from backend.core.config import DEMO_USER_ID
from backend.planner.contracts import Plan, Step

# 仅用于演示和缺省兜底。正常请求通过 build(..., account_id=...) 传入按身份解析的账户。
ACCOUNT_ID = 1


def _amount(v) -> str:
    return f"{Decimal(str(v)):.2f}" if v is not None else ""


def _missing_plan(intent: str, missing: list[str], slots: dict) -> Plan:
    return Plan(intent=intent, slots=slots, steps=[], missing=missing)


def build(intent: str, slots: dict, account_id: int = ACCOUNT_ID) -> Plan:
    """意图 + 槽位 → 模板 DAG。

    `account_id` 必须由调用方按**当前主体**解析后传入（见 services.context.account_of）：
    写死成 1 会让第二个客户的钱从第一个客户的账户进出。
    """
    s = dict(slots)
    acct = int(account_id or ACCOUNT_ID)
    amt = s.get("amount")
    when = s.get("when") or {}

    # ---------------- 转账 ----------------
    if intent in ("transfer.create", "transfer.schedule"):
        missing = []
        if not s.get("payee_raw"):
            missing.append("payee")
        if amt is None:
            missing.append("amount")
        if intent == "transfer.schedule" and not when.get("date"):
            missing.append("when")
        if missing:
            return _missing_plan(intent, missing, s)

        steps = [
            Step(id="s1", tool="payee.search", args={"q": s["payee_raw"]}, label=f"检索收款人「{s['payee_raw']}」"),
            Step(id="s2", tool="account.balance", args={"account_id": acct}, label="查询可用余额"),
        ]
        if intent == "transfer.create":
            steps.append(Step(id="s3", tool="transfer.create",
                              args={"account_id": acct, "payee_id": "$s1.chosen",
                                    "amount": _amount(amt), "memo": s.get("memo") or ""},
                              label="发起转账", on_fail="abort"))
        else:
            first_run = f"{when['date']} {when.get('time') or '09:00'}:00"
            steps.append(Step(id="s3", tool="transfer.schedule",
                              args={"account_id": acct, "payee_id": "$s1.chosen",
                                    "amount": _amount(amt), "memo": s.get("memo") or "",
                                    "first_run_at": first_run,
                                    "recurrence": when.get("recurrence") or "once"},
                              label="设置定时转账"))
        return Plan(intent=intent, slots=s, steps=steps)

    # ---------------- AA 收款 ----------------
    if intent == "transfer.collect":
        missing = []
        if amt is None:
            missing.append("amount")
        if not s.get("people"):
            missing.append("people")
        if missing:
            return _missing_plan(intent, missing, s)
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="transfer.collect",
                 args={"total": _amount(amt), "people": int(s["people"]),
                       "memo": s.get("memo") or "AA 收款"}, label="生成 AA 收款单"),
        ])

    # ---------------- 余额 ----------------
    if intent == "balance.query":
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="account.list", label="查询账户"),
        ])

    # ---------------- 账单 ----------------
    if intent in ("bill.summary", "bill.anomaly"):
        p = s.get("period") or {}
        if not p.get("from"):
            return _missing_plan(intent, ["period"], s)
        steps = []
        if intent == "bill.anomaly":
            steps.append(Step(id="s1", tool="bill.anomalies", args={"from": p["from"], "to": p["to"]},
                              label=f"扫描{p['label']}异常交易"))
            steps.append(Step(id="s2", tool="bill.summary", args={"from": p["from"], "to": p["to"]},
                              label="汇总消费结构"))
        else:
            steps.append(Step(id="s1", tool="bill.summary", args={"from": p["from"], "to": p["to"]},
                              label=f"汇总{p['label']}账单"))
        return Plan(intent=intent, slots=s, steps=steps)

    # ---------------- 理财 ----------------
    if intent == "wealth.compare":
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="wealth.products", args={"risk_max": "R3", "term_max": 9999},
                 label="按风险等级筛选货架"),
        ])

    if intent == "wealth.subscribe":
        missing = []
        if amt is None:
            missing.append("amount")
        if not s.get("product_keyword"):
            missing.append("product")
        if missing:
            return _missing_plan(intent, missing, s)
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="wealth.products",
                 args={"risk_max": "R3", "term_max": 9999, "keyword": s["product_keyword"]},
                 label=f"匹配产品「{s['product_keyword']}」"),
            Step(id="s2", tool="wealth.order",
                 args={"product_id": "$s1.chosen", "amount": _amount(amt)}, label="申购"),
        ])

    if intent == "wealth.redeem":
        missing = []
        if not s.get("product_keyword"):
            missing.append("product")
        if missing:
            return _missing_plan(intent, missing, s)
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="wealth.holdings", args={"keyword": s["product_keyword"]},
                 label=f"匹配持仓「{s['product_keyword']}」"),
            Step(id="s2", tool="wealth.redeem",
                 args={"holding_id": "$s1.chosen", **({"amount": _amount(amt)} if amt else {})},
                 label="赎回"),
        ])

    # ---------------- 卡片 ----------------
    if intent in ("card.limit", "card.freeze", "card.loss"):
        steps = [Step(id="s1", tool="card.list", args={"last4": s.get("card_last4") or ""}, label="定位卡片")]
        if intent == "card.limit":
            if amt is None:
                return _missing_plan(intent, ["daily_limit"], s)
            steps.append(Step(id="s2", tool="card.limit",
                              args={"card_id": "$s1.chosen", "daily_limit": _amount(amt)}, label="调整限额"))
        elif intent == "card.freeze":
            # 方向取结构化槽位 `freeze`；memo 只是给用户看的备注，不参与判定
            want_freeze = s.get("freeze")
            if want_freeze is None:                    # 兼容旧调用方（补槽路径可能没带）
                want_freeze = s.get("memo") != "解冻"
            steps.append(Step(id="s2", tool="card.freeze",
                              args={"card_id": "$s1.chosen", "freeze": bool(want_freeze)},
                              label="冻结卡片" if want_freeze else "解冻卡片"))
        else:
            steps.append(Step(id="s2", tool="card.loss", args={"card_id": "$s1.chosen"},
                              label="挂失卡片", on_fail="abort"))
        return Plan(intent=intent, slots=s, steps=steps)

    if intent == "card.apply":
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="card.apply", args={"card_type": "标准白金卡"}, label="提交办卡申请"),
        ])

    if intent == "card.list":
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="card.list", args={"last4": s.get("card_last4") or ""}, label="查询卡片"),
        ])

    # ---------------- 订阅 ----------------
    if intent == "sub.list":
        return Plan(intent=intent, slots=s, steps=[Step(id="s1", tool="sub.list", label="扫描订阅扣款")])

    if intent == "sub.cancel":
        kw = s.get("merchant_keyword")
        if not kw:
            return _missing_plan(intent, ["merchant"], s)
        return Plan(intent=intent, slots=s, steps=[
            Step(id="s1", tool="sub.list", args={"keyword": kw}, label=f"匹配订阅「{kw}」"),
            Step(id="s2", tool="sub.cancel", args={"sub_id": "$s1.chosen"}, label="取消订阅"),
        ])

    # ---------------- 跨场景联动 ----------------
    if intent == "cross.birthday":
        budget = s.get("budget") or "800"
        expire = when.get("date") or None
        return Plan(intent=intent, slots={**s, "budget": budget}, steps=[
            Step(id="s1", tool="hold.create",
                 args={"amount": _amount(budget), "reason": "生日礼物专项资金",
                       **({"expire_at": expire} if expire else {})},
                 label=f"锁定专项资金 {_amount(budget)} 元", on_fail="compensate"),
            Step(id="s2", tool="gift.recommend", args={"budget": _amount(budget)}, label="推荐礼物"),
        ])
    return _missing_plan(intent, [], s)
