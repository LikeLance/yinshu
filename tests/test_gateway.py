"""工具网关：白名单、参数校验、模糊匹配、幂等、本地参数不下发。

网关是"编排层唯一出口"，它松一寸，前面所有安全设计就漏一寸。
"""
from __future__ import annotations

import pytest

from backend.core.errors import LimitExceeded, YinshuError
from backend.tools import gateway, registry
from backend.tools.gateway import ToolArgError, _pick


def test_unknown_tool_rejected():
    with pytest.raises(KeyError):
        registry.get("transfer.delete_everything")


def test_unknown_argument_rejected():
    spec = registry.get("account.list")
    with pytest.raises(ToolArgError):
        gateway.validate(spec, {"user_id": 1, "evil_extra": "x"})


def test_missing_required_argument():
    spec = registry.get("transfer.create")
    with pytest.raises(ToolArgError) as ei:
        gateway.validate(spec, {})
    assert "缺少必填" in str(ei.value)


def test_defaults_are_applied():
    spec = registry.get("transfer.create")
    out = gateway.validate(spec, {"payee_id": 1, "amount": "10", "account_id": 1})
    assert out["payee_id"] == 1


def test_fuzzy_pick_strips_colloquial_suffix():
    items = [{"id": 1, "merchant": "腾讯视频VIP"}, {"id": 2, "merchant": "网易云音乐黑胶"}]
    data: dict = {}
    _pick(items, "merchant", "腾讯视频订阅", data)
    assert data["chosen"] == 1


def test_fuzzy_pick_marks_ambiguous_when_two_match():
    items = [{"id": 1, "merchant": "腾讯视频VIP"}, {"id": 2, "merchant": "腾讯视频超级影视"}]
    data: dict = {}
    _pick(items, "merchant", "腾讯视频", data)
    assert data.get("ambiguous") is True and data.get("chosen") is None
    assert len(data["options"]) == 2


def test_local_param_not_sent_downstream():
    """keyword 只用于本地收敛，银行接口并不认识它——不能带过去。"""
    res = gateway.call("sub.list", {"keyword": "iCloud"})
    assert res["chosen"] and "iCloud" in res["picked"]["merchant"]


def test_idempotency_same_key_same_result():
    args = {"account_id": 1, "payee_id": 1, "amount": "12.34", "memo": "幂等测试"}
    a = gateway.call("transfer.create", args, trace_id="fixed-trace", step_id="s1")
    b = gateway.call("transfer.create", args, trace_id="fixed-trace", step_id="s1")
    assert a["transfer_id"] == b["transfer_id"]
    assert b["idempotent_replay"] is True


def test_limit_raises_business_error():
    with pytest.raises((LimitExceeded, YinshuError)):
        gateway.call("transfer.create", {"account_id": 1, "payee_id": 1, "amount": "999999"})


def test_blacklisted_payee_blocked_by_core():
    with pytest.raises(YinshuError):
        gateway.call("transfer.create", {"account_id": 1, "payee_id": 6, "amount": "10"})


def test_connection_pool_is_reused():
    c1 = gateway.client()
    c2 = gateway.client()
    assert c1 is c2
    gateway.close()
    assert gateway.client() is not c1
