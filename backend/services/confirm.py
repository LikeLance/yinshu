"""确认态：把"用户看到的"和"将要执行的"锁在一起。

这是最容易出事的一段：
- 预览结果最多挂 5 分钟，期间世界会变（余额 / 黑名单 / 频率 / 设备）；
- 用户可能连点两次确认，也可能两个 worker 同时收到确认。

所以确认时必须重新 preview + 重算指纹 + 重新决策，并用跨进程 CAS 抢占执行权。
"""
from __future__ import annotations

import uuid
from typing import Any

from backend.core.errors import YinshuError
from backend.planner import executor, fingerprint
from backend.planner.contracts import Plan
from backend.presenter import build as build_presentation, confirm_rows
from backend.security import audit, policy
from backend.services.context import (build_context, compute_drift, risk_stats,
                                       take_snapshot)
from backend.services.view import build_response, guard_check, notice
from backend.session import fsm


def stash_pending(s: fsm.Session, plan: Plan, out: executor.Outcome, ctx: policy.Context,
                   d: policy.Decision, slots: dict, intent: str, inj: bool) -> None:
    """把"待确认事务"连同指纹、快照、版本号一起落盘。"""
    fsm.await_confirm(s, {
        "v": 2,
        "fp": fingerprint.of(plan, slots, out.results),
        "ver": s.ver,
        "confirm_id": uuid.uuid4().hex,
        "plan": plan, "out": out, "decision": d, "slots": slots,
        "intent": intent, "inj": inj,
        "snapshot": take_snapshot(slots, out, ctx, d),
        "double": d.require_double_confirm, "confirm_n": 0,
    })


def confirm_card(plan: Plan, slots: dict, out: executor.Outcome, d: policy.Decision,
                  extra_rows: list[list[str]] | None = None,
                  note: str = "点确认后才会真正执行；说「取消」随时终止") -> dict:
    rows = confirm_rows(plan, slots, out)
    if extra_rows:
        rows = extra_rows + rows
    return {"type": "confirm", "title": "请确认这笔操作", "rows": rows,
            "reasons": d.reasons, "risk": d.risk, "tier": d.tier,
            "need_double": d.require_double_confirm, "note": note}


# ---------------- 主处理 ----------------


def execute_pending(s: fsm.Session, trace: str, t0: float, p: Any = None) -> dict:
    """确认后执行。

    **不信任**落盘的预览快照：先重新预览，再重新决策，然后比对漂移。
    原实现直接拿最多 5 分钟前的 out 去 commit，期间余额变化、收款人被拉黑、
    频率特征上升都不会被重新评估——"权限下沉到执行层"在确认这一跳失守。
    """
    stash = s.stash
    plan: Plan = stash["plan"]
    slots: dict = stash["slots"]
    intent: str = stash["intent"]
    inj = bool(stash.get("inj"))
    d_old: policy.Decision = stash.get("decision") or policy.decide(
        build_context(plan, slots, stash.get("out") or executor.Outcome(), risk_stats(), inj, s))
    confirm_id = str(stash.get("confirm_id") or uuid.uuid4().hex)

    # 1) 重新预览：只读步骤重跑，确认"世界还是那个世界"
    fresh = executor.Outcome()
    try:
        executor.run(plan, "preview", trace, fresh, principal=p)
    except YinshuError as e:
        audit.append(trace, "recheck_failed", {"code": e.code}, "error")
        fsm.reset(s)
        msg = e.user_msg or "执行前复核没通过，请重新发起。"
        return build_response(s, trace, msg, notice("danger", "已终止", [msg]),
                     {"error": e.code, "rechecked": True}, t0)

    # 2) 重算指纹 + 重新决策 + 比对漂移
    fp2 = fingerprint.of(plan, slots, fresh.results)
    ctx2 = build_context(plan, slots, fresh, risk_stats(), inj, s)
    d2 = policy.decide(ctx2)
    snap2 = take_snapshot(slots, fresh, ctx2, d2)
    drift = compute_drift(stash.get("snapshot") or {}, snap2, stash.get("fp"), fp2, d_old, d2)
    audit.append(trace, "confirm_recheck",
                 {"fp_changed": fp2 != (stash.get("fp") or fp2),
                  "hard": drift["changes"], "soft": drift["soft"],
                  "tier": f"{d_old.tier}->{d2.tier}"},
                 "stale" if drift["hard"] else "ok")

    if drift["hard"]:
        # 用最新状态重建确认卡：用户看到的就是"即将执行"的那一版
        stash_pending(s, plan, fresh, ctx2, d2, slots, intent, inj)
        card = confirm_card(plan, slots, fresh, d2,
                             extra_rows=[["已变化", c] for c in drift["changes"]],
                             note="情况有变，请重新确认；说「取消」随时终止")
        return build_response(s, trace, "这笔的状态发生了变化，需要您重新确认一次。", card,
                     {"stale": True, "changes": drift["changes"],
                      "decision": d2.to_dict()}, t0)

    # 3) 抢占：用户连点两次 / 两个 worker 同时收到确认，只有一个能真正执行
    if not fsm.claim(s, p, {**stash, "fp": fp2, "snapshot": snap2}):
        audit.append(trace, "confirm_duplicate", {}, "ok")
        return build_response(s, trace, "这笔已经在处理了，请勿重复确认。",
                     notice("info", "处理中", ["同一笔只会执行一次"]),
                     {"duplicate_confirm": True}, t0)

    # 4) 执行：幂等种子用 confirm_id，跨重试稳定
    try:
        out = executor.run(plan, "commit", trace, fresh, principal=p, idem_seed=confirm_id)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code}, "error")
        fsm.reset(s)
        msg = e.user_msg or "执行失败，请稍后再试。"
        return build_response(s, trace, msg, notice("danger", "执行未完成", [msg]),
                     {"error": e.code, "compensated": fresh.compensated}, t0)

    text_out, card, extra = build_presentation(intent, plan, out, d2, slots)
    bad = guard_check(text_out, card, executor.facts(slots, out.results, extra))
    if bad:
        text_out = "操作已完成，详细结果见下方卡片。"
        audit.append(trace, "guard_rewrite", {"violations": bad}, "ok")
    if drift["soft"]:
        audit.append(trace, "confirm_drift", {"soft": drift["soft"]}, "ok")
        if isinstance(card, dict):
            card = {**card, "drift": drift["soft"]}
    audit.append(trace, "execute_ok", {"intent": intent, "confirm_id": confirm_id,
                                       "compensated": fresh.compensated}, "ok")
    fsm.finish(s, "EXECUTED")
    fsm.reset(s)
    return build_response(s, trace, text_out, card,
                 {"decision": d2.to_dict(), "guard_violations": bad,
                  "compensated": fresh.compensated, "drift": drift["soft"]}, t0)
