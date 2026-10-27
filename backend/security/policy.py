"""权限策略引擎 —— 本项目的核心差异化。

三个设计原则：
1. **代码级，不是提示词级**：模型说什么都不影响结果，只有这里的白纸黑字算数；
2. **可解释**：每一分都对应一句人话，直接渲染进确认卡片；
3. **默认往安全侧**：拿不准就升级到 CONFIRM，而不是放行。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from backend.core.config import POLICY_FILE

Tier = Literal["AUTO", "CONFIRM", "HANDOFF", "DENY"]

_cfg_cache: dict | None = None
_cfg_mtime: float = 0.0


def config() -> dict:
    global _cfg_cache, _cfg_mtime
    m = POLICY_FILE.stat().st_mtime
    if _cfg_cache is None or m != _cfg_mtime:
        _cfg_cache = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        _cfg_mtime = m
    return _cfg_cache


@dataclass
class Context:
    action: str
    amount: Decimal = Decimal("0")
    payee_blacklisted: bool = False
    payee_whitelisted: bool = False
    payee_new: bool = True
    payee_relation: str = ""
    cross_border: bool = False
    device_trusted: bool = True
    hour: int = 12
    velocity_1h: int = 0
    daily_count: int = 0
    segment: str = "普通"
    wealth_unsuitable: bool = False
    injection_flagged: bool = False


@dataclass
class Decision:
    tier: Tier
    risk: int
    reasons: list[str] = field(default_factory=list)
    require_double_confirm: bool = False
    hard_rule: str = ""

    @property
    def ok(self) -> bool:
        return self.tier in ("AUTO", "CONFIRM")

    def to_dict(self) -> dict:
        return {"tier": self.tier, "risk": self.risk, "reasons": self.reasons,
                "require_double_confirm": self.require_double_confirm, "hard_rule": self.hard_rule}


def score(ctx: Context) -> tuple[int, list[str]]:
    cfg = config()
    s = cfg["weights"].get(ctx.action, 15)
    why: list[str] = [f"基础风险：{ctx.action}（{s} 分）"]
    f = cfg["features"]

    def bump(cond: bool, key: str, text: str) -> None:
        nonlocal s
        if cond:
            s += f[key]
            why.append(text)

    # 收款人相关特征只对"转出去"的动作有意义，别把卡片的分数算歪
    if ctx.action.startswith("transfer"):
        bump(not ctx.payee_whitelisted, "payee_not_whitelisted", "收款人不在常转名单内")
        bump(ctx.payee_new, "payee_new", "近 30 天无与该收款人的交易")
    bump(ctx.amount > Decimal("5000"), "amount_gt_5000", "单笔金额超过 5,000 元")
    bump(ctx.amount > Decimal("20000"), "amount_gt_20000", "单笔金额超过 20,000 元")
    bump(not ctx.device_trusted, "device_untrusted", "非常用设备发起")
    bump(0 <= ctx.hour < 6, "hour_night", "凌晨 0-6 点操作")
    bump(ctx.velocity_1h >= 5, "velocity_1h_ge_5", "1 小时内已发生 5 次以上资金操作")
    bump(ctx.segment == "老年", "segment_elderly", "适老保护策略生效")
    bump(ctx.action.startswith("wealth") and ctx.amount > Decimal("10000"),
         "wealth_amount_gt_10000", "理财大额划出（>1 万元）")
    return min(s, 100), why


def decide(ctx: Context) -> Decision:
    cfg = config()
    t = cfg["tiers"]

    # ---- 硬规则优先于打分 ----
    if ctx.payee_blacklisted:
        return Decision("DENY", 100, ["收款人命中反诈黑名单，为保护您的资金已拒绝"], hard_rule="blacklist")
    if ctx.wealth_unsuitable:
        return Decision("DENY", 100, ["该产品风险等级高于您的风险承受能力（适当性管理）"],
                        hard_rule="unsuitable")
    if ctx.amount > Decimal("50000") and ctx.action.startswith("transfer"):
        return Decision("HANDOFF", 85, ["单笔金额超过 5 万元，按监管要求需人工复核"],
                        hard_rule="amount_gt_50000")
    if ctx.cross_border:
        return Decision("HANDOFF", 80, ["收款账户为跨境账户，需人工审核"], hard_rule="cross_border")

    risk, why = score(ctx)
    if risk <= t["auto_max"]:
        tier: Tier = "AUTO"
    elif risk <= t["confirm_max"]:
        tier = "CONFIRM"
    else:
        tier = "HANDOFF"

    # ---- 注入降级：检测到可疑指令，一律往安全侧挪一档 ----
    if ctx.injection_flagged and cfg.get("injection_downgrade", True):
        order: list[Tier] = ["AUTO", "CONFIRM", "HANDOFF"]
        i = order.index(tier)
        if i < len(order) - 1:
            tier = order[i + 1]
            why.append("本次会话出现可疑指令，已自动提高确认等级")

    dbl = ctx.action in cfg["hard_rules"].get("double_confirm", [])
    return Decision(tier, risk, why, require_double_confirm=dbl)


def matrix() -> list[dict]:
    """给前端/文档展示的决策矩阵。"""
    t = config()["tiers"]
    return [
        {"range": f"0–{t['auto_max']}", "tier": "AUTO", "ui": "直接执行 → 回执卡片"},
        {"range": f"{t['auto_max'] + 1}–{t['confirm_max']}", "tier": "CONFIRM", "ui": "确认卡片 + 列明命中原因"},
        {"range": f"{t['confirm_max'] + 1}–100", "tier": "HANDOFF", "ui": "转人工，生成工单号"},
    ]
