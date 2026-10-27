"""多租户隔离：身份从请求上下文来，数据按主体分，**跨用户一律失败关闭**。

这一组用例的存在本身比数量重要：多租户最容易出的不是"功能没做"，
而是"悄悄跨用户动钱"。所以每条都断言"另一个客户的数据/账户没被动"。
"""
from __future__ import annotations

import sqlite3

import httpx
import pytest

from backend.core import config
from backend.security import identity
from backend.security.identity import Principal
from backend.tools import gateway


@pytest.fixture(autouse=True)
def _reset_account_cache():
    from backend.services.context import forget_account

    for uid in (1, 2):
        forget_account(uid)
    yield
    for uid in (1, 2):
        forget_account(uid)


def _balance(account_id: int) -> str:
    c = sqlite3.connect(str(config.BANK_DB))
    try:
        return c.execute("SELECT balance FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    finally:
        c.close()


def _uid2() -> Principal:
    return Principal(uid=2, device_id="dev-tenant-2", channel="app")


# ---------------------------------------------------------------- 读隔离

def test_accounts_are_scoped_to_caller():
    mine = gateway.call("account.list", {}, principal=_uid2())
    ids = [a["id"] for a in mine["accounts"]]
    assert ids == [3], ids                      # 只有客户 2 自己的活期账户

    other = gateway.call("account.list", {}, principal=Principal(1, "dev-1"))
    assert 1 in [a["id"] for a in other["accounts"]]
    assert 3 not in [a["id"] for a in other["accounts"]]


def test_payees_are_scoped_to_caller():
    mine = gateway.call("payee.search", {"q": "韩梅梅"}, principal=_uid2())
    assert mine["payee"]["id"] == 8

    with pytest.raises(Exception):
        gateway.call("payee.search", {"q": "李慧"}, principal=_uid2())


def test_query_param_cannot_spoof_identity(bank_url):
    """`?user_id=1` 是可伪造的，身份只能来自请求上下文。"""
    r = httpx.get(f"{bank_url}/v1/accounts", params={"user_id": 1},
                  headers={"X-User-Id": "2"}, trust_env=False, timeout=5)
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == [3]


def test_bills_read_callers_own_account():
    from datetime import date, timedelta

    to = date.today().isoformat()
    frm = (date.today() - timedelta(days=30)).isoformat()
    data = gateway.call("bill.summary", {"from": frm, "to": to}, principal=_uid2())
    # 客户 2 没有任何流水，读到的必须是空账，而不是客户 1 的
    assert data["total_out"] == "0.00" and data["count"] == 0


# ---------------------------------------------------------------- 写隔离（失败关闭）

def test_cannot_transfer_from_foreign_account():
    with pytest.raises(Exception) as ei:
        gateway.call("transfer.create",
                     {"account_id": 1, "payee_id": 8, "amount": "100.00", "memo": "越权"},
                     "t-forbid-1", "s3", principal=_uid2(), idem_seed="forbid-1")
    assert "FORBIDDEN" in str(ei.value) or "不属于当前用户" in str(ei.value)


def test_cannot_transfer_to_foreign_payee():
    with pytest.raises(Exception) as ei:
        gateway.call("transfer.create",
                     {"account_id": 3, "payee_id": 1, "amount": "100.00", "memo": "越权"},
                     "t-forbid-2", "s3", principal=_uid2(), idem_seed="forbid-2")
    assert "FORBIDDEN" in str(ei.value) or "不属于当前用户" in str(ei.value)


def test_cannot_operate_foreign_card():
    with pytest.raises(Exception) as ei:
        gateway.call("card.limit", {"card_id": 1, "daily_limit": "999.00"},
                     "t-card", "s2", principal=_uid2())
    assert "FORBIDDEN" in str(ei.value) or "不属于当前用户" in str(ei.value)


def test_own_transfer_moves_only_own_money(clean_bank):
    before_mine, before_other = _balance(3), _balance(1)
    r = gateway.call("transfer.create",
                     {"account_id": 3, "payee_id": 8, "amount": "100.00", "memo": "自己的钱"},
                     "t-own", "s3", principal=_uid2(), idem_seed="own-1")
    assert r["transfer_id"]

    mine = float(_balance(3))
    assert abs(float(before_mine) - mine - 100.0) < 0.01
    assert _balance(1) == before_other, "客户 1 的钱被动了"


# ---------------------------------------------------------------- 端到端：规划器按主体取账户

def test_planner_resolves_account_per_caller(clean_bank, monkeypatch):
    """客户 2 说"转 100 给韩梅梅"，落地必须是账户 3，而不是写死的账户 1。"""
    import json

    from backend.main import ChatReq, handle_chat

    # AUTH_MODE 默认 off（演示模式），必须显式打开才会按令牌取主体
    monkeypatch.setattr(config, "AUTH_MODE", "optional")
    tok = identity.issue_token(2, "dev-tenant-2")
    h = {"Authorization": f"Bearer {tok}"}
    before_mine, before_other = _balance(3), _balance(1)

    d = json.loads(handle_chat(ChatReq(text="给韩梅梅转100"), headers=h).body)
    sid = d["session_id"]
    assert d["card"]["type"] in ("confirm", "receipt"), d
    if d["card"]["type"] == "confirm":
        d = json.loads(handle_chat(ChatReq(session_id=sid, text="确认"), headers=h).body)
    assert d["card"]["type"] == "receipt", d

    assert float(_balance(3)) < float(before_mine)
    assert _balance(1) == before_other


def test_auth_mode_optional_requires_matching_owner(client, clean_bank, monkeypatch):
    """打开鉴权后，客户 2 的会话不能被客户 1 的令牌接手。"""
    monkeypatch.setattr(config, "AUTH_MODE", "optional")
    tok2 = identity.issue_token(2, "dev-tenant-2")
    tok1 = identity.issue_token(1, "dev-1")

    first = client.post("/api/chat", json={"text": "给韩梅梅转100"},
                        headers={"Authorization": f"Bearer {tok2}"}).json()
    sid = first["session_id"]
    assert first["card"]["type"] == "confirm"

    bad = client.post("/api/chat", json={"session_id": sid, "text": "确认"},
                      headers={"Authorization": f"Bearer {tok1}"})
    assert bad.status_code == 403
    assert bad.json()["error"]["code"] == "SESSION_OWNER_MISMATCH"
