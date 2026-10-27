"""风险上下文：把"世界状态"翻译成权限引擎能吃的 Context。

风险特征是权限决策的唯一输入，这一层要保证：
- 拿不到的信息往安全侧取（不认识 = 陌生收款人）；
- 脏数据不能让整条链路 500（风险等级未知就不做适当性判定，而不是抛异常）；
- 快照只记可比较的标量，供确认阶段做漂移比对。
"""
from __future__ import annotations

from decimal import Decimal

from backend.core import clock
from backend.core.errors import YinshuError
from backend.planner import executor
from backend.planner.contracts import Plan
from backend.security import policy
from backend.services.view import first_of
from backend.tools import gateway


RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
CUSTOMER_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}


def risk_stats() -> dict:
    try:
        return gateway.call("risk.stats", {})
    except YinshuError:
        return {}


# ---------------- 决策上下文 ----------------


def build_context(plan: Plan, slots: dict, out: executor.Outcome, stats: dict, inj: bool,
             s: fsm.Session) -> policy.Context:
    action = plan.primary_action() or "transfer.create"
    amt = Decimal("0")
    for k in ("amount", "budget", "daily_limit"):
        if slots.get(k) not in (None, "", []):
            try:
                amt = Decimal(str(slots[k]))
                break
            except Exception:
                pass

    payee = ((out.results.get("s1") or {}).get("payee") or {})
    pid = payee.get("id")
    known = stats.get("known_payees") or []
    recent = stats.get("recent_payees") or []

    unsuitable = False
    if action == "wealth.order":
        picked = (out.results.get("s1") or {}).get("picked") or {}
        level = stats.get("risk_level", "C3")
        risk = picked.get("risk")
        if risk in RISK_ORDER:                    # 脏数据不能让整条链路 500
            unsuitable = RISK_ORDER[risk] > CUSTOMER_ORDER.get(level, 3)

    return policy.Context(
        action=action, amount=amt,
        payee_blacklisted=bool(payee.get("blacklisted")),
        payee_whitelisted=pid in known if pid else True,
        # 拿不到收款人时按"陌生收款人"处理（往安全侧偏）；
        # 原实现是 False，等于悄悄少加 10 分。
        payee_new=(pid not in recent) if pid else True,
        payee_relation=payee.get("relation") or "",
        # 跨境与否由核心系统给出的标记决定。
        # 原实现是 `"HSBC" in bank` —— 汇丰在内地也有法人银行，这个判断既会漏也会误伤。
        cross_border=bool(payee.get("cross_border")),
        device_trusted=s.device_trusted,
        hour=clock.now().hour,
        velocity_1h=stats.get("velocity_1h", 0),
        daily_count=stats.get("daily_count", 0),
        segment=stats.get("segment", "普通"),
        wealth_unsuitable=unsuitable,
        injection_flagged=inj,
    )


def take_snapshot(slots: dict, out: executor.Outcome, ctx: policy.Context,
              d: policy.Decision) -> dict:
    """只记"可比较的标量"，不把整份 Outcome 落盘。

    整份落盘有两个问题：体积大；更重要是"确认时读到的是旧对象"，等于把
    TOCTOU 漏洞换个地方藏起来。
    """
    payee = first_of(out.results, "payee") or {}
    return {
        "payee_id": payee.get("id"),
        "payee_name": payee.get("name"),
        "blacklisted": bool(payee.get("blacklisted")),
        "balance": first_of(out.results, "balance"),
        "amount": str(ctx.amount),
        "velocity_1h": ctx.velocity_1h,
        "daily_count": ctx.daily_count,
        "segment": ctx.segment,
        "risk": d.risk,
        "tier": d.tier,
    }


def compute_drift(old: dict, new: dict, fp_old: str | None, fp_new: str,
           d_old: policy.Decision, d_new: policy.Decision) -> dict:
    """比对预览时与确认时的世界状态，分级处置。

    HARD = 用户看到的对象/金额变了，或风险升级 → 必须重新确认；
    SOFT = 只有频率类特征波动 → 照常执行，但留痕并提示。
    不做"任何变化都拒绝"：余额从 9000 涨到 9500 不该打断用户。
    """
    hard: list[str] = []
    soft: list[str] = []

    if fp_old and fp_new and fp_old != fp_new:
        hard.append("执行参数已变化（金额/收款人/时间等）")
    if old.get("payee_id") != new.get("payee_id"):
        hard.append(f"收款人变了：{old.get('payee_name')} → {new.get('payee_name')}")
    if not old.get("blacklisted") and new.get("blacklisted"):
        hard.append("收款人已被列入反诈黑名单")
    if d_new.tier in ("DENY", "HANDOFF") and d_old.tier != d_new.tier:
        hard.append(f"权限等级变化：{d_old.tier} → {d_new.tier}")
    if d_new.risk - d_old.risk >= 15:
        hard.append(f"风险分上升：{d_old.risk} → {d_new.risk}")

    try:
        bal, amt = Decimal(str(new["balance"])), Decimal(str(new["amount"]))
        if amt > 0 and bal < amt:
            hard.append(f"可用余额不足：{bal:.2f} 元 < 待执行 {amt:.2f} 元")
    except Exception:                                          # noqa: BLE001
        pass

    for key, label in (("velocity_1h", "1 小时内资金操作次数"),
                       ("daily_count", "当日累计操作次数"),
                       ("segment", "客户分层")):
        if old.get(key) != new.get(key):
            soft.append(f"{label}：{old.get(key)} → {new.get(key)}")
    if not hard and old.get("balance") != new.get("balance"):
        soft.append(f"可用余额：{old.get('balance')} → {new.get('balance')}")

    return {"hard": bool(hard), "changes": hard, "soft": soft}
