"""意图识别：规则打分版（确定性、可离线、可解释）。

为什么不用模型做这一步：规则版给出的是可复现的结果，答辩现场不会因为
一次采样抖动而演示失败；模型路径保留在 planner/llm.py 里，作为可选增强。
"""
from __future__ import annotations

from backend.nlu.slots import (parse_amount, parse_card_last4, parse_keyword_after, parse_memo,
                               parse_payee, parse_people, parse_period, parse_when)

INTENTS = [
    "transfer.create", "transfer.schedule", "transfer.collect",
    "bill.summary", "bill.anomaly",
    "wealth.compare", "wealth.subscribe", "wealth.redeem",
    "card.apply", "card.limit", "card.freeze", "card.loss", "card.list",
    "sub.list", "sub.cancel",
    "cross.birthday", "gift.order", "balance.query",
]

INTENT_LABEL = {
    "transfer.create": "智能转账", "transfer.schedule": "定时转账", "transfer.collect": "AA 收款",
    "bill.summary": "账单分析", "bill.anomaly": "异常交易识别",
    "wealth.compare": "理财对比", "wealth.subscribe": "理财申购", "wealth.redeem": "理财赎回",
    "card.apply": "卡片申请", "card.limit": "额度调整", "card.freeze": "卡片冻结",
    "card.loss": "卡片挂失", "card.list": "卡片查询",
    "sub.list": "订阅代扣管理", "sub.cancel": "取消订阅",
    "cross.birthday": "跨场景联动", "gift.order": "礼物下单", "balance.query": "余额查询",
    "unknown": "未识别",
}

CONFIRM_WORDS = {"确认", "确定", "好的", "好", "可以", "是", "是的", "对", "行", "转吧", "办吧",
                 "ok", "okay", "嗯", "同意", "执行", "就这个", "第一个", "第二个", "第三个"}
CANCEL_WORDS = {"取消", "算了", "不用了", "不要", "停止", "别", "不了", "再想想"}


def parse(text: str) -> dict:
    """返回 {intent, confidence, slots}。slots 里空缺的字段由 planner 决定是否追问。"""
    t = (text or "").strip()
    low = t.lower()
    when = parse_when(t)
    amount = parse_amount(t)

    def out(intent: str, conf: float = 0.9, **slots) -> dict:
        base = {"period": None, "payee_raw": None, "amount": None, "memo": None,
                "when": when if when["explicit"] else None, "people": None,
                "product_keyword": None, "card_last4": parse_card_last4(t),
                "merchant_keyword": None, "who": None}
        base.update(slots)
        return {"intent": intent, "confidence": conf, "slots": base}

    # 1) 跨场景联动
    if ("生日" in t or "礼物" in t) or ("花" in t and any(k in t for k in ("订", "买", "送"))):
        who = "母亲" if any(k in t for k in ("妈", "母亲")) else \
              "父亲" if any(k in t for k in ("爸", "父亲")) else None
        return out("cross.birthday", 0.93, who=who)

    # 2) AA 收款
    if "aa" in low or "平摊" in t or "各付" in t or "分摊" in t:
        return out("transfer.collect", 0.92, amount=amount, people=parse_people(t),
                   memo=parse_memo(t) or "AA 收款")

    # 3/4) 订阅代扣
    sub_kw = ("订阅", "自动续费", "代扣", "自动扣款", "会员")
    if "退订" in t or any(k in t for k in ("取消", "关掉", "关了", "关一下", "停掉", "停用")):
        if "退订" in t or any(k in t for k in sub_kw):
            return out("sub.cancel", 0.9, merchant_keyword=parse_keyword_after(
                t, ("取消", "退订", "关掉", "关闭", "关了", "关一下", "停用", "停掉")) or
                parse_keyword_after(t, ("订阅",)))
    if any(k in t for k in sub_kw) or "续费" in t:
        return out("sub.list", 0.9)

    # 5-8) 卡片
    if "挂失" in t:
        return out("card.loss", 0.95)
    if any(k in t for k in ("冻结", "解冻", "锁卡")):
        return out("card.freeze", 0.93, memo="解冻" if "解冻" in t else "冻结")
    if any(k in t for k in ("额度", "限额")):
        return out("card.limit", 0.9, amount=amount)
    if ("申请" in t or "办一张" in t or "办个" in t) and "卡" in t:
        return out("card.apply", 0.9)
    if "尾号" in t or ("卡" in t and any(k in t for k in ("状态", "哪些卡", "几张卡", "卡列表", "多少卡", "查卡"))):
        return out("card.list", 0.9)

    # 9) 赎回
    if "赎回" in t or ("取出来" in t):
        return out("wealth.redeem", 0.9, amount=amount,
                   product_keyword=parse_keyword_after(t, ("赎回", "把")))

    # 10) 申购
    if any(k in t for k in ("买", "申购", "买入", "投")) and (amount or any(k in t for k in ("理财", "基金", "产品", "债"))):
        return out("wealth.subscribe", 0.88, amount=amount,
                   product_keyword=parse_keyword_after(t, ("买", "申购", "投")))

    # 11) 理财对比
    if any(k in t for k in ("理财", "稳健", "收益", "基金", "产品", "投资", "投什么", "能投")):
        return out("wealth.compare", 0.88)

    # 12/13) 账单
    if any(k in t for k in ("异常", "奇怪", "盗刷", "不对劲", "可疑", "乱扣")):
        return out("bill.anomaly", 0.92, period=parse_period(t))
    if any(k in t for k in ("账单", "花了", "花哪", "花在", "消费", "支出", "开销", "报告", "花费", "钱都")):
        return out("bill.summary", 0.92, period=parse_period(t))

    # 14) 余额
    if "余额" in t or ("多少钱" in t and any(k in t for k in ("卡", "账户", "里"))):
        return out("balance.query", 0.9)

    # 15) 转账（区分定时 / 即时）
    if any(k in t for k in ("转", "汇", "打钱", "付款", "付给")):
        intent = "transfer.schedule" if (when["explicit"] and (when["recurrence"] or when["date"])) else "transfer.create"
        return out(intent, 0.9, payee_raw=parse_payee(t), amount=amount, memo=parse_memo(t))

    return out("unknown", 0.4)


def is_confirm(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t == w or t.startswith(w) for w in CONFIRM_WORDS)


def is_cancel(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t == w or t.startswith(w) for w in CANCEL_WORDS)
