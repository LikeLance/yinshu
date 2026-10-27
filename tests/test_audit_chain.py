"""审计链的并发与可追责用例。

这几条是 P0-1 的验收标准：老实现（模块级单连接 + 进程内锁）在这些用例下会分叉。
"""
from __future__ import annotations

import sqlite3
import threading

from backend.core.config import APP_DB
from backend.security import audit, pii


def _raw(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    c = sqlite3.connect(str(APP_DB), timeout=5.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def test_concurrent_appends_keep_chain_linear():
    """20 线程 × 25 次并发写，链必须仍然是一条直线。

    老实现里 20 个线程共用一把进程内锁还能撑住，真正的杀手是多进程；
    这里同时验证了 per-thread 连接 + BEGIN IMMEDIATE 没有引入新的断链。
    """
    threads, per = 20, 25
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(per):
                audit.append(f"conc-{i}-{j}", "concurrent_probe",
                             {"i": i, "j": j}, "ok")
        except BaseException as e:          # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"并发写报错：{errors[:3]}"
    v = audit.verify(full=True)
    assert v["ok"] is True, v
    assert v["checked"] == v["total"]

    # 链的首要性质：每个 prev_hash 恰好被下一行引用一次，没有分叉
    rows = _raw("SELECT prev_hash FROM audit_logs ORDER BY id")
    prevs = [r["prev_hash"] for r in rows]
    assert prevs[0] == audit.GENESIS
    assert len(set(prevs)) == len(prevs), "出现分叉：同一 prev_hash 被多行引用"


def test_payload_is_redacted_before_persisting():
    ref = audit.append("pii-probe", "user_input",
                       {"text": "给我妈转500，卡号 6222021234567890，手机 13800138000",
                        "payee_name": "张明"}, "ok")
    assert ref["pii_ref"], "含敏感字段时必须写 pii_vault"

    row = _raw("SELECT payload FROM audit_logs WHERE id=?", (ref["id"],))[0]
    body = row["payload"]
    assert "6222021234567890" not in body
    assert "13800138000" not in body
    assert "张明" not in body
    # 用户原话整体替换为长度标记：审计要知道"说了多长"，不需要知道"说了什么"
    assert "<redacted len=42>" in body
    assert "张*" in body          # 姓名保留姓，便于人工核对对象

    vault = _raw("SELECT digest,key_id FROM pii_vault WHERE ref=?", (ref["pii_ref"],))[0]
    assert len(vault["digest"]) == 64          # HMAC-SHA256，不可逆
    assert vault["key_id"] == pii.key_id()


def test_verify_detects_tampering_and_recovers():
    ref = audit.append("tamper-probe", "policy_decision", {"tier": "AUTO"}, "ok")
    row = _raw("SELECT payload_hash,self_hash FROM audit_logs WHERE id=?", (ref["id"],))[0]
    original_ph = row["payload_hash"]

    conn = sqlite3.connect(str(APP_DB), timeout=5.0, isolation_level=None)
    try:
        conn.execute("UPDATE audit_logs SET payload_hash=? WHERE id=?",
                     ("f" * 64, ref["id"]))
        broken = audit.verify(full=True)
        assert broken["ok"] is False and broken["broken_at"] == ref["id"]

        conn.execute("UPDATE audit_logs SET payload_hash=? WHERE id=?", (original_ph, ref["id"]))
    finally:
        conn.close()

    assert audit.verify(full=True)["ok"] is True


def test_anchor_tail_matches_chain_head():
    audit.append("anchor-probe", "read_only", {"intent": "balance.query"}, "ok")
    res = audit.anchor_tail()
    assert res["ok"] is True

    v = audit.verify()
    assert v["anchor"] is not None
    assert v["anchor"]["match"] is True, "锚点 head 与本地链尾不一致"
    assert v["anchor"]["seq"] == res["seq"]


def test_principal_dimensions_are_recorded():
    class P:
        uid = 42
        device_id = "dev-x"
        ip = "10.0.0.9"
        channel = "app"

        @property
        def actor_name(self) -> str:
            return "user:42"

    ref = audit.append("principal-probe", "read_only", {"intent": "bill.summary"},
                       "ok", principal=P())
    row = _raw("SELECT actor,actor_id,device_id,ip,channel,chain_ver FROM audit_logs WHERE id=?",
               (ref["id"],))[0]
    assert row["actor_id"] == 42
    assert row["device_id"] == "dev-x"
    assert row["ip"] == "10.0.0.9"
    assert row["actor"] == "user:42"
    assert row["chain_ver"] == 2
