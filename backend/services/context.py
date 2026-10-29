"""风险上下文：将业务状态转换为权限引擎使用的 Context。

风险特征是权限决策的输入，这一层需要保证：
- 缺失信息按安全侧处理，例如未知收款人视为陌生收款人；
- 下游数据异常时返回降级状态，不将异常当作低风险；
- 快照只保存可比较的标量，供确认阶段做漂移检查。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from backend.core import clock
from backend.core.errors import YinshuError
from backend.planner import executor
from backend.planner.contracts import Plan
from backend.security import policy
from backend.services.view import first_of
from backend.tools import gateway


RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
CUSTOMER_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}


def risk_stats() -> tuple[dict, bool]:
    """取风控特征，并明确告知"这次到底拿到没有"。

    原来异常时静默返回 {}，调用方就会把速度/客群当成"0 次、普通客户"，
    于是下游一超时策略反而更宽松。把可用性一起返回，由策略引擎把 AUTO 降级为 CONFIRM。
    """
    try:
        data = gateway.call("risk.stats", {})
        return (data if isinstance(data, dict) else {}), True
    except YinshuError:
        return {}, False


# ---------------- 决策上下文 ----------------


def build_context(plan: Plan, slots: dict, out: executor.Outcome, stats: dict, inj: bool,
             s: fsm.Session, degraded: bool = False) -> policy.Context:
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
        degraded=degraded,
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


# ---------------- 账户解析（多租户） ----------------

_ACCT_CACHE: dict[int, tuple[float, int]] = {}
_ACCT_TTL = 60.0


def account_of(principal: Any = None) -> int:
    """当前主体的默认扣款账户。

    原实现把 `ACCOUNT_ID = 1` 写死在规划器里，第二个客户的钱会从
    第一个客户的账户进出。账户是 per-user 的数据，必须按主体解析。

    60 秒缓存：一次对话里可能连续规划多轮，避免每轮都打一次核心系统；
    代价是"刚开户立刻转账"可能拿到旧值，所以失败时兜底到演示账户，
    资金操作执行前，核心系统还会校验资源归属，校验失败时拒绝执行。
    """
    from backend.core import config, reqctx

    p = principal if principal is not None else reqctx.principal()
    owner = int(getattr(p, "uid", 0) or config.DEMO_USER_ID)
    hit = _ACCT_CACHE.get(owner)
    if hit and time.time() - hit[0] < _ACCT_TTL:
        return hit[1]

    # 只兜"下游不可用"，不兜编程错误：把 TypeError/ValueError 也吞掉，
    # 会让"少传一个参数"这种 bug 静默退化成"用兜底账户"，而兜底账户是用户 1 的。
    try:
        data = gateway.call("account.list", {})
    except YinshuError:
        data = {}
    accounts = data.get("accounts") or []
    picked = next((a for a in accounts if a.get("kind") == "活期"),
                  accounts[0] if accounts else None)
    aid = int(picked["id"]) if picked else config.DEMO_ACCOUNT_ID
    _ACCT_CACHE[owner] = (time.time(), aid)
    return aid


def forget_account(owner: int) -> None:
    """测试用：清掉账户缓存。"""
    _ACCT_CACHE.pop(owner, None)
