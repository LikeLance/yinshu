"""受控流程：多步确认、候选选择、补偿、调试接口。

这些路径一旦挂掉就是资金事故，必须逐个走通。
"""
from __future__ import annotations


def test_card_loss_full_double_confirm(client):
    ask = client.post("/api/chat", json={"text": "把卡挂失"}).json()
    assert ask["card"]["type"] == "choose"          # 名下有卡 → 先问哪张
    sid = ask["session_id"]

    picked = client.post("/api/chat", json={"session_id": sid, "text": "第1个"}).json()
    assert picked["card"]["type"] == "confirm"
    assert picked["card"]["need_double"] is True
    assert picked["card"]["risk"] >= 60

    once = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert once["card"]["type"] == "notice"         # 二次确认提示
    twice = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert twice["card"]["type"] == "receipt"
    assert "挂失" in twice["reply"]


def test_gift_flow_locks_funds_then_orders(client):
    plan = client.post("/api/chat", json={"text": "下周三我妈生日，帮我准备一下"}).json()
    assert plan["card"]["type"] == "choose"
    assert len(plan["card"]["options"]) >= 2
    sid = plan["session_id"]

    confirm = client.post("/api/chat", json={"session_id": sid, "text": "第2个"}).json()
    assert confirm["card"]["type"] == "confirm"
    done = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert done["card"]["type"] == "receipt"
    assert "订单号" in done["reply"] or "已下单" in done["reply"]


def test_subscription_cancel_flow(client):
    listed = client.post("/api/chat", json={"text": "有哪些自动续费"}).json()
    assert listed["card"]["type"] == "subs"
    assert listed["card"]["annual_total"]

    start = client.post("/api/chat", json={"text": "取消腾讯视频订阅"}).json()
    assert start["card"]["type"] == "confirm"
    sid = start["session_id"]
    done = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert done["card"]["type"] == "receipt"
    assert "取消" in done["reply"]


def test_wealth_redeem_flow(client):
    r = client.post("/api/chat", json={"text": "赎回安心债基 100 元"}).json()
    assert r["debug"]["intent"] in ("wealth.redeem", "wealth.compare")
    assert r["card"]["type"] in ("confirm", "receipt", "notice", "wealth")


def test_plan_preview_endpoint(client):
    r = client.get("/api/plan/给我妈转500").json()
    assert r["intent"] == "transfer.create"
    assert r["steps"] and r["steps"][0]["tool"]
    assert r["missing"] == []


def test_audit_trace_lookup(client):
    body = client.post("/api/chat", json={"text": "这个月钱都花哪了"}).json()
    trace = body["trace_id"]
    logs = client.get(f"/api/audit/{trace}").json()
    assert logs["trace_id"] == trace
    assert len(logs["logs"]) >= 2                # 至少 user_input + response


def test_confirm_after_expiry_is_rejected(client, monkeypatch):
    from backend.session import fsm

    first = client.post("/api/chat", json={"text": "给我妈转8000"}).json()
    sid = first["session_id"]

    # 把确认窗口调成负数 → 立即过期
    monkeypatch.setattr(fsm, "CONFIRM_TTL", -1)
    out = client.post("/api/chat", json={"session_id": sid, "text": "确认"}).json()
    assert out["debug"].get("expired") is True
    assert "超时" in out["reply"]


def test_unknown_payee_is_reported_not_guessed(client):
    r = client.post("/api/chat", json={"text": "给张三转100"}).json()
    assert r["card"]["type"] == "notice"
    assert "没找到" in r["reply"]
