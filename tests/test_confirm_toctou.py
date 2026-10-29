"""确认阶段的 TOCTOU 防护（P0-2）。

核心命题：用户点「确认」时执行的，必须是他看到的那一笔；期间世界变了就要重新确认，
而不是把 5 分钟前的快照直接 commit 出去。
"""
from __future__ import annotations

import sqlite3
import time

from backend.core import config
from backend.planner import fingerprint
from backend.planner.contracts import Plan, Step


def _bank_exec(sql: str, params: tuple = ()) -> None:
    c = sqlite3.connect(str(config.BANK_DB), timeout=5.0, isolation_level=None)
    try:
        c.execute(sql, params)
    finally:
        c.close()


def test_state_change_forces_reconfirm(client):
    """预览后余额被花掉 → 确认时必须重新确认，而不是照旧扣款。"""
    first = client.post("/api/chat", json={"text": "给我妈转8000"}).json()
    assert first["card"]["type"] == "confirm"
    sid = first["session_id"]

    # 模拟"这 5 分钟里钱被花掉了"
    _bank_exec("UPDATE accounts SET balance='1.00' WHERE id=1")

    out = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert out["debug"].get("stale") is True, out
    assert out["card"]["type"] == "confirm"
    assert out["card"]["need_double"] == first["card"]["need_double"] or "need_double" in out["card"]
    changes = " ".join(str(x) for x in out["debug"]["changes"])
    assert "余额不足" in changes or "变化" in changes

    # 状态没变的话，第二次确认应当能正常执行（用新会话避免幂等重放干扰）
    _bank_exec("UPDATE accounts SET balance='50000.00' WHERE id=1")
    retry = client.post("/api/chat", json={"session_id": out["session_id"], "text": "确认"}).json()
    assert retry["card"]["type"] == "receipt", retry


def test_claim_is_exclusive_across_sessions():
    """同一笔待确认事务，只能被抢占一次（防连点 / 多 worker 并发）。"""
    from backend.security.identity import Principal
    from backend.session import fsm

    p = Principal.demo()
    s = fsm.get(None, p)
    fsm.await_confirm(s, {"amount": 1, "created_at": time.time()})

    a = fsm.get(s.sid, p)
    b = fsm.get(s.sid, p)
    assert fsm.claim(a, p, {"amount": 1}) is True
    assert fsm.claim(b, p, {"amount": 1}) is False

    fsm.reset(a)


def test_fingerprint_binds_resolved_args_not_just_refs():
    """指纹必须跟着"解析后的收款人"变，而不是只看 Plan 里的 $s1.chosen。"""
    plan = Plan(intent="transfer.create", slots={},
                steps=[Step(id="s3", tool="transfer.create",
                            args={"account_id": 1, "payee_id": "$s1.chosen",
                                  "amount": "8000.00", "memo": ""})])
    fp_a = fingerprint.of(plan, {}, {"s1": {"chosen": 11}})
    fp_b = fingerprint.of(plan, {}, {"s1": {"chosen": 22}})
    assert fp_a != fp_b, "换了收款人指纹却没变 → 确认卡与执行可以不一致"

    fp_a2 = fingerprint.of(plan, {}, {"s1": {"chosen": 11}})
    assert fp_a == fp_a2, "同样输入必须得到同样指纹"


def test_fingerprint_tracks_amount_change():
    plan = Plan(intent="transfer.create", slots={},
                steps=[Step(id="s3", tool="transfer.create",
                            args={"account_id": 1, "payee_id": 11, "amount": "8000.00"})])
    other = Plan(intent="transfer.create", slots={},
                 steps=[Step(id="s3", tool="transfer.create",
                             args={"account_id": 1, "payee_id": 11, "amount": "800.00"})])
    assert fingerprint.of(plan, {}, {}) != fingerprint.of(other, {}, {})


def test_duplicate_confirm_returns_same_receipt_without_second_transfer(client):
    """连续提交两次确认时，第二次应被幂等回放或返回处理中，不产生第二笔转账。"""
    from backend.core import clock

    start = client.post("/api/chat", json={"text": "给我妈转8000"}).json()
    sid = start["session_id"]
    assert start["card"]["type"] == "confirm"

    c = sqlite3.connect(str(config.BANK_DB))
    before = int(c.execute("SELECT COUNT(*) FROM transfers").fetchone()[0])
    c.close()

    h1 = {"X-Idempotency-Key": f"click-1-{clock.now().timestamp()}"}
    h2 = {"X-Idempotency-Key": f"click-2-{clock.now().timestamp()}"}
    r1 = client.post("/api/chat", json={"session_id": sid, "text": "确认"}, headers=h1).json()
    r2 = client.post("/api/chat", json={"session_id": sid, "text": "确认"}, headers=h2).json()

    c = sqlite3.connect(str(config.BANK_DB))
    after = int(c.execute("SELECT COUNT(*) FROM transfers").fetchone()[0])
    c.close()

    assert r1["card"]["type"] == "receipt"
    assert r2["card"]["type"] in ("notice", "receipt")
    assert after - before == 1, "同一笔被扣了两次"
