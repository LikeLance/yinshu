"""会话存储：类型还原、金额精度、TTL、撤销即删。

这是"重启不丢待确认那一笔"的底层保证，必须单独测。
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from backend.planner.contracts import Plan, Step
from backend.planner.executor import Outcome
from backend.security.policy import Decision
from backend.session import fsm
from backend.session.store import SqliteStore, decode_stash, encode_stash


@pytest.fixture()
def store(tmp_path):
    st = SqliteStore(tmp_path / "session.db")
    old = fsm.STORE
    fsm.configure(st)
    yield st
    fsm.configure(old)
    st.close()


def test_roundtrip_keeps_types_and_decimal_precision(tmp_path):
    plan = Plan(intent="transfer.create", slots={"amount": Decimal("8000.00")},
                steps=[Step(id="s1", tool="account.list")])
    stash = {"plan": plan,
             "out": Outcome(results={"s1": {"chosen": 1}}, executed=["s1"]),
             "decision": Decision(tier="CONFIRM", risk=35, reasons=["基础风险"]),
             "intent": "transfer.create",
             "slots": {"amount": Decimal("8000.00")},
             "confirm_n": 1, "double": True, "created_at": time.time()}

    raw = encode_stash(stash)
    back = decode_stash(raw)

    assert isinstance(back["plan"], Plan)
    assert back["plan"].steps[0].tool == "account.list"
    assert isinstance(back["out"], Outcome)
    assert back["out"].results["s1"]["chosen"] == 1
    assert isinstance(back["decision"], Decision)
    assert back["decision"].tier == "CONFIRM" and back["decision"].risk == 35
    # 金额必须还是 Decimal，且一分不差
    assert isinstance(back["slots"]["amount"], Decimal)
    assert back["slots"]["amount"] == Decimal("8000.00")
    assert str(back["slots"]["amount"]) == "8000.00"
    assert back["confirm_n"] == 1 and back["double"] is True


def test_state_survives_new_store_instance(tmp_path):
    p = tmp_path / "s.db"
    a = SqliteStore(p)
    a.save("s1", "AWAIT_CONFIRM", {"amount": Decimal("1.00")}, [], True)
    b = SqliteStore(p)                       # 新实例 = 模拟进程重启
    rec = b.load("s1")
    assert rec["state"] == "AWAIT_CONFIRM"
    assert decode_stash(rec["stash"])["amount"] == Decimal("1.00")
    a.close()
    b.close()


def test_purge_expired(store):
    store.save("old", "IDLE", {}, [], True)
    assert store.purge(ttl=-1) == 1          # ttl 为负 → 全部视为过期
    assert store.count() == 0


def test_reset_removes_pending_transaction(store):
    s = fsm.get("s-reset")
    fsm.await_confirm(s, {"amount": Decimal("500"), "created_at": time.time()})
    assert store.count() == 1
    fsm.reset(s)
    assert store.count() == 0                # 撤销后重启也不会再冒出来


def test_idle_session_not_persisted(store):
    s = fsm.get("s-idle")
    fsm.save(s)
    assert store.count() == 0                # 只读往返不写库


def test_expired_session_is_new(store):
    store.save("s-exp", "AWAIT_CONFIRM", {"created_at": time.time()}, [], True)
    # 手动把 updated 改老
    with store._lock:                        # noqa: SLF001 —— 测试内部状态
        store._conn.execute("UPDATE sessions SET updated=? WHERE sid=?", (time.time() - 10**6, "s-exp"))
        store._conn.commit()
    s = fsm.get("s-exp")
    assert s.state == "IDLE" and s.stash == {}
