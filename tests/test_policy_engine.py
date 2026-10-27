"""权限引擎：矩阵用例逐条复算，以及"为什么是这个分"的可解释性。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.security import policy

CASES = json.loads((Path(__file__).parent / "cases" / "policy_matrix.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["desc"] for c in CASES])
def test_matrix_matches_decide(case):
    ctx = policy.Context(**{k: v for k, v in case.items()
                            if k not in ("desc", "expect", "amount")},
                         amount=Decimal(str(case.get("amount", "0"))))
    assert policy.decide(ctx).tier == case["expect"]


def test_blacklist_short_circuits_everything():
    d = policy.decide(policy.Context(action="transfer.create", amount=Decimal("1"),
                                     payee_blacklisted=True, injection_flagged=True))
    assert d.tier == "DENY" and d.hard_rule == "blacklist"
    assert d.risk == 100


def test_injection_never_lowers_tier():
    base = policy.Context(action="transfer.create", amount=Decimal("100"),
                          payee_whitelisted=True, payee_new=False)
    safe = policy.decide(base)
    flagged = policy.decide(policy.Context(**{**base.__dict__, "injection_flagged": True}))
    order = {"AUTO": 0, "CONFIRM": 1, "HANDOFF": 2, "DENY": 3}
    assert order[flagged.tier] >= order[safe.tier]
    assert any("可疑指令" in r for r in flagged.reasons)


def test_reasons_are_human_readable_and_complete():
    ctx = policy.Context(action="transfer.create", amount=Decimal("8000"),
                         payee_whitelisted=False, payee_new=True, device_trusted=False, hour=3)
    d = policy.decide(ctx)
    assert d.reasons and all(isinstance(r, str) and len(r) > 4 for r in d.reasons)
    # 金额相关的分值必须真的出现在理由里
    assert any("8,000" in r or "8000" in r or "5,000" in r or "5000" in r for r in d.reasons)


def test_amount_bump_is_monotonic():
    lo = policy.score(policy.Context(action="transfer.create", amount=Decimal("1000")))[0]
    hi = policy.score(policy.Context(action="transfer.create", amount=Decimal("9000")))[0]
    assert hi > lo


def test_payee_features_do_not_affect_card_actions():
    """收款人相关加分只对转账有意义，别把挂失的分算歪。"""
    a = policy.score(policy.Context(action="card.loss", amount=Decimal("0"),
                                    payee_new=True, payee_whitelisted=False))[0]
    b = policy.score(policy.Context(action="card.loss", amount=Decimal("0"),
                                    payee_new=False, payee_whitelisted=True))[0]
    assert a == b


def test_matrix_covers_all_tiers_and_ranges_are_contiguous():
    rows = policy.matrix()
    tiers = [r["tier"] for r in rows]
    assert tiers == ["AUTO", "CONFIRM", "HANDOFF"]
    t = policy.config()["tiers"]
    assert rows[0]["range"].endswith(str(t["auto_max"]))
    assert rows[1]["range"].startswith(str(t["auto_max"] + 1))
    assert rows[2]["range"].startswith(str(t["confirm_max"] + 1))
