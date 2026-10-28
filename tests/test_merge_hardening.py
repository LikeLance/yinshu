"""融合上游 v1.1.0 之后补的几处缺口。

上游已经把编排层加固得很到位（TOCTOU 重算、审计链多进程 CAS、幂等键分域、多租户、PII 脱敏、
统一时钟），这一批只补它还没覆盖的部分，逐条对应一个"会真的出事"的场景。
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from starlette.requests import Request

from backend.core import config
from backend.security import policy
from backend.services import context as ctx_mod
from backend.session import fsm


# ---------------- 风控特征缺失：不许 fail-open ----------------

def test_missing_risk_features_cannot_stay_auto():
    """风控下游一超时，风险分反而变小 —— 这是最典型的 fail-open。"""
    base = {"action": "transfer.create", "amount": Decimal("100"),
            "payee_whitelisted": True, "payee_new": False}
    assert policy.decide(policy.Context(**base)).tier == "AUTO"

    d = policy.decide(policy.Context(**base, degraded=True))
    assert d.tier == "CONFIRM"
    assert any("风控特征" in r for r in d.reasons)


def test_risk_stats_reports_availability(monkeypatch):
    """取不到特征时必须让上层知道，而不是返回一个空 dict 装没事。"""
    from backend.core.errors import ToolFailed

    def boom(*a, **kw):
        raise ToolFailed("down")

    monkeypatch.setattr(ctx_mod.gateway, "call", boom)
    data, ok = ctx_mod.risk_stats()
    assert data == {} and ok is False


# ---------------- 过期待确认事务必须真删 ----------------

def test_expired_pending_is_deleted_from_store(client, monkeypatch):
    """只清内存不删库 → 之后每次带同一个 sid 进来都又判一次超时。"""
    first = client.post("/api/chat", json={"text": "给我妈转8000"}).json()
    sid = first["session_id"]
    assert fsm.STORE.load(sid) is not None

    monkeypatch.setattr(fsm, "CONFIRM_TTL", -1)
    out = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert out["debug"].get("expired") is True
    assert fsm.STORE.load(sid) is None          # 库里那条必须真的没了


# ---------------- 锁资金也要过闸门 ----------------

def test_fund_hold_is_gated_before_confirmation():
    """锁资金不转钱，但它占用可用余额；gated=False 会让整条链路被判成"纯只读"。"""
    from backend.planner import planner
    from backend.tools import registry

    assert registry.get("hold.create").is_gated is True
    plan = planner.build("cross.birthday", {"budget": "800"})
    assert plan.primary_action() == "hold.create"


# ---------------- 冻结方向是槽位，不是备注 ----------------

def test_freeze_direction_is_a_slot_not_a_memo():
    from backend.nlu import intent as nlu
    from backend.planner import planner

    assert nlu.parse("冻结尾号6621的卡")["slots"]["freeze"] is True
    assert nlu.parse("解冻尾号6621的卡")["slots"]["freeze"] is False

    # 显式槽位优先：备注里带"解冻"也不能把方向翻过来
    plan = planner.build("card.freeze", {"freeze": True, "memo": "解冻", "card_last4": "6621"})
    assert plan.steps[1].args["freeze"] is True
    # 旧调用方仍然兼容
    legacy = planner.build("card.freeze", {"memo": "解冻", "card_last4": "6621"})
    assert legacy.steps[1].args["freeze"] is False


# ---------------- 注册表自检 ----------------

def test_registry_invariants_hold_and_catch_mistakes():
    from backend.tools import registry

    registry.assert_invariants()          # 当前注册表必须自洽

    bad = registry.ToolSpec("x.pay", "POST", "/v1/x/{id}", {"amount": str}, ("amount",),
                            money=True, reversible=False)   # 资金类但没开幂等，且占位符没参数
    registry.TOOLS["x.pay"] = bad
    try:
        with pytest.raises(registry.RegistryInvariantError) as ei:
            registry.assert_invariants()
        msg = str(ei.value)
        assert "idem" in msg and "占位符" in msg
    finally:
        registry.TOOLS.pop("x.pay", None)


def test_confirm_card_marks_irreversible_actions():
    """`reversible` 元数据以前只写在注册表里没人读，现在要真的出现在确认卡上。"""
    from backend.services.confirm import irreversible_steps
    from backend.planner import planner

    assert irreversible_steps(planner.build("sub.cancel", {"merchant_keyword": "腾讯"}))
    assert irreversible_steps(planner.build("card.limit",
                                            {"amount": "30000", "card_last4": "6621"})) == []


# ---------------- 账单：金额不碰浮点 ----------------

def test_bill_summary_adds_up_in_decimal(bank_url):
    """原实现 SUM(CAST(amount AS REAL))：先转二进制浮点再求和，与"绝不碰浮点"自相矛盾。"""
    with httpx.Client(base_url=bank_url, trust_env=False) as c:
        s = c.get("/v1/bills/summary", params={"from": "2000-01-01", "to": "2100-01-01"}).json()
    total = Decimal(s["total_out"])
    assert sum(Decimal(x["amount"]) for x in s["categories"]) == total
    for x in s["categories"]:
        assert len(x["amount"].split(".")[1]) == 2


# ---------------- 核心系统：写端点必须消费幂等键 ----------------

def test_wealth_order_is_idempotent_at_core(bank_url):
    """网关带了 Idempotency-Key，核心系统却直接丢掉 —— 一次超时重试就是一笔重复申购。"""
    hdr = {"Idempotency-Key": "merge-wealth-001"}
    body = {"product_id": "P03", "amount": "200.00"}
    with httpx.Client(base_url=bank_url, trust_env=False) as c:
        before = len(c.get("/v1/wealth/holdings").json()["holdings"])
        a = c.post("/v1/wealth/orders", json=body, headers=hdr)
        b = c.post("/v1/wealth/orders", json=body, headers=hdr)
        after = len(c.get("/v1/wealth/holdings").json()["holdings"])
    assert a.status_code == 200, a.text
    assert b.json().get("idempotent_replay") is True
    assert after == before + 1


def test_keyless_write_is_rejected(bank_url):
    with httpx.Client(base_url=bank_url, trust_env=False) as c:
        r = c.post("/v1/partner/gift/order", json={"gift_id": "G01"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_REQUIRED"


# ---------------- 基础设施 ----------------

def test_xff_is_ignored_unless_proxy_is_trusted(monkeypatch):
    """默认信 XFF 等于把限流做成装饰品：换个请求头就是新身份。"""
    from backend.core import middleware as mw

    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                   "scheme": "http", "server": ("t", 80), "client": ("9.9.9.9", 1),
                   "headers": [(b"x-forwarded-for", b"1.2.3.4")]})
    monkeypatch.setattr(config, "TRUST_PROXY", False)
    assert mw._client_key(req) == "9.9.9.9"
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    assert mw._client_key(req) == "1.2.3.4"


def test_metric_labels_collapse_identifiers():
    from backend.core import middleware as mw

    def label(path: str) -> str:
        return mw._route_label(Request({"type": "http", "method": "GET", "path": path,
                                        "query_string": b"", "scheme": "http",
                                        "server": ("t", 80), "client": ("1.1.1.1", 1),
                                        "headers": []}))

    assert label("/api/audit/9f2c1e0b7a4d4c88") == "/api/audit/{id}"
    assert label("/api/v1/health") == "/api/v1/health"


def test_rate_limit_buckets_do_not_grow_forever():
    from backend.core import middleware as mw

    rl = mw.RateLimit(app=None, rpm=60)
    rl.MAX_BUCKETS = 100
    rl._ops = 255                          # 下一次调用正好触发回收
    for i in range(500):
        rl._buckets[f"10.0.0.{i}"] = (59.0, 0.0)
    rl._evict(10_000_000.0)
    assert len(rl._buckets) == 0
