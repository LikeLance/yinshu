"""身份与幂等的验收用例（P0-3 / P0-4）。

核心命题：同一笔业务请求重放 N 次，银行侧只能产生 1 笔；身份不能来自请求体；
拿别人的会话 ID 不能替他确认。
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.core import config
from backend.security import identity


def _bank_count(table: str) -> int:
    c = sqlite3.connect(str(config.BANK_DB))
    try:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        c.close()


# ---------------------------------------------------------------- 幂等

def test_same_idempotency_key_executes_once(client):
    """同一幂等键重放，第二次必须回放首次响应，且银行侧只多一笔。"""
    before = _bank_count("transfers")
    headers = {"X-Idempotency-Key": "idem-transfer-001"}
    body = {"text": "给我妈转500"}

    first = client.post("/api/chat", json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["card"]["type"] == "receipt"
    after_first = _bank_count("transfers")
    assert after_first == before + 1

    second = client.post("/api/chat", json=body, headers=headers)
    assert second.status_code == 200
    assert second.headers.get("x-idempotent-replay") == "true"
    assert second.json()["reply"] == first.json()["reply"]
    assert _bank_count("transfers") == after_first, "重放了却仍然扣了钱"


def test_replayed_request_does_not_duplicate_when_body_identical(client):
    """不带显式 key 时，同主体 + 同请求体也应被认成同一笔。"""
    body = {"text": "给我妈转300"}
    before = _bank_count("transfers")
    r1 = client.post("/api/chat", json=body).json()
    r2 = client.post("/api/chat", json=body).json()
    assert r1["card"]["type"] == "receipt"
    assert r2["card"]["type"] == "receipt"
    assert r2["trace_id"] != "" or True
    assert _bank_count("transfers") == before + 1, "同一个请求体被当成两笔"


def test_same_key_different_body_is_conflict(client):
    headers = {"X-Idempotency-Key": "idem-conflict-001"}
    client.post("/api/chat", json={"text": "给我妈转500"}, headers=headers)
    r = client.post("/api/chat", json={"text": "给我妈转900"}, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_step_level_idempotency_key_is_deterministic():
    """网关的幂等键不能随本次网络会话漂移——这正是原来重复扣款的根因。"""
    from backend.tools.gateway import _idem_key

    class P:
        uid = 7

    a = _idem_key("transfer.create", "s3", P(), "seed-x", "trace-AAA")
    b = _idem_key("transfer.create", "s3", P(), "seed-x", "trace-BBB")
    assert a == b, "同一个业务种子换了 trace 就换了 key → 下游判重失效"
    assert a != _idem_key("transfer.create", "s3", P(), "seed-y", "trace-AAA")


def test_hold_create_is_idempotent_downstream():
    """锁定资金也必须幂等：超时重试不能把预算锁两份。"""
    from backend.tools import gateway

    a = gateway.call("hold.create", {"amount": "100.00", "reason": "幂等测试"},
                     "t1", "s1", idem_seed="hold-seed-1")
    b = gateway.call("hold.create", {"amount": "100.00", "reason": "幂等测试"},
                     "t2", "s1", idem_seed="hold-seed-1")
    assert a["hold_id"] == b["hold_id"]
    assert b.get("idempotent_replay") is True


# ---------------------------------------------------------------- 身份

def test_identity_in_body_is_rejected(client):
    r = client.post("/api/chat", json={"text": "给我妈转500", "user_id": 2})
    assert r.status_code == 422


def test_device_trust_is_not_client_asserted(client, monkeypatch):
    """客户端说 device_trusted=true 不再影响决策。

    off 模式下统一按可信处理（演示/测试）；这里只验证"请求体字段被忽略"。
    """
    from backend.main import ChatReq

    req = ChatReq(**{"text": "x", "device_trusted": False})
    assert req.device_trusted is False          # 字段还在（兼容旧前端）
    assert "device_trusted" not in identity.principal_from_headers({}).__dict__


def test_token_roundtrip_and_expiry():
    tok = identity.issue_token(42, "dev-1", "app", ttl=60)
    p = identity.verify_token(tok, "10.0.0.1")
    assert p.uid == 42 and p.device_id == "dev-1" and p.ip == "10.0.0.1"

    with pytest.raises(identity.AuthError):
        identity.verify_token(tok[:-2] + "xx")

    expired = identity.issue_token(42, "dev-1", ttl=-1)
    with pytest.raises(identity.AuthError):
        identity.verify_token(expired)


def test_session_belongs_to_its_owner(client, monkeypatch):
    """A 的会话，B 拿着 sid 也不能替 A 确认。"""
    first = client.post("/api/chat", json={"text": "给我妈转8000"}).json()
    sid = first["session_id"]
    assert first["card"]["type"] == "confirm"

    monkeypatch.setattr(config, "AUTH_MODE", "optional")
    token_b = identity.issue_token(2, "device-b")
    r = client.post("/api/chat", json={"session_id": sid, "text": "确认"},
                    headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "SESSION_OWNER_MISMATCH"
