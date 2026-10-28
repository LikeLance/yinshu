"""金额与日期的解析边界。

这几条都是"会真的把钱算错 / 把日期办错"的路径，不是措辞优化：

- 「转一百零五块」曾被解析成 150 元；
- 「26号给我妈转500」的日期曾被静默丢掉（分支不可达）；
- 「每月31号」的首次执行日曾落在**已经过去**的日子；
- 幻觉校验只认阿拉伯数字，把金额写成中文就能绕过；
- 赎回时关键词对不上，网关曾自动挑第一个持仓去卖。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.core import cnnum
from backend.nlu import intent as nlu
from backend.nlu import slots
from backend.planner import executor
from backend.security import guard
from backend.tools import gateway


# ---------------- 中文数字：一份实现 ----------------

def test_chinese_numerals_have_one_implementation():
    """NLU 与幻觉校验曾经各写一份，只修了一处 —— 同一句话两个答案。"""
    cases = ["一百零五", "一千零八", "一万二", "两百五", "十五", "五百",
             "壹仟贰佰", "两万", "三十", "一百二"]
    for c in cases:
        assert slots.cn2num(c) == cnnum.to_int(c) == guard.cn_to_number(c), c

    assert cnnum.to_int("一百零五") == 105      # "零"后面是个位真值，不能按口语省略读
    assert cnnum.to_int("一千零八") == 1008
    assert cnnum.to_int("一万二") == 12000      # 口语省略
    assert cnnum.to_int("壹仟贰佰") == 1200     # 大写金额


def test_amount_with_zero_placeholder_is_not_read_as_ellipsis():
    assert nlu.parse("转一百零五块")["slots"]["amount"] == Decimal("105")
    assert nlu.parse("转一千二块")["slots"]["amount"] == Decimal("1200")


# ---------------- 只说了「几号」 ----------------

def test_day_only_date_is_parsed():
    today = date(2026, 10, 29)
    assert slots.parse_when("26号给我妈转500", today)["date"] == "2026-11-26"
    assert slots.parse_when("下个月26号转500", today)["date"] == "2026-11-26"
    # 「尾号6621」不是日期，不能把挂失说成定时
    assert slots.parse_when("挂失尾号6621的卡", today)["explicit"] is False


def test_monthly_first_run_is_always_in_the_future():
    today = date(2026, 10, 29)
    w = slots.parse_when("每月31号给房东转2500", today)
    assert w["date"] == "2026-10-31" and w["recurrence"] == "monthly:31"
    assert slots.parse_when("每月1号给房东转2500", today)["date"] == "2026-11-01"
    # 2 月没有 31 号 → 落到月末，而不是落到"上个月"
    assert slots.parse_when("每月31号转100", date(2027, 2, 1))["date"] == "2027-02-28"


# ---------------- 幻觉校验：中文数字与日期 ----------------

def test_guard_catches_chinese_numerals():
    assert guard.check("已转出 500 元", {"500"}) == []
    # 白名单里有 500 时，「五百」是通过的（同一数值）—— 这正是要的语义
    assert guard.check("已转出五百元", {"500"}) == []
    bad = guard.check("已转出五百元", {"200"})
    assert bad and "中文数字:五百" in bad[0]
    # 没有量级单位的词不算数字，别把正常话术判成违规
    assert guard.check("请一次性确认", set()) == []


def test_guard_and_facts_share_the_same_number_rules():
    """取数与研判必须同一套规则，否则白名单和校验各说各话。"""
    assert guard.numbers_in("2026-10-28 22:43:06") == []
    assert guard.check("完成于 2026-10-28 22:43:06", set()) == []

    acc = executor.facts({}, {"s1": {"ts": "2026-10-28 22:43:06", "amount": "120.00"}})
    assert "2026" not in acc and "43" not in acc
    assert "120" in acc or "120.00" in acc


# ---------------- 赎回：不许猜 ----------------

def test_redeem_unknown_product_never_falls_back_to_first_holding():
    data = {"holdings": [{"id": 1, "name": "安心债基 A"}, {"id": 2, "name": "银枢活期+"}]}
    miss = gateway._post("wealth.holdings", dict(data), {"keyword": "黄金ETF"})
    assert miss.get("chosen") is None and miss.get("not_found") is True

    hit = gateway._post("wealth.holdings", dict(data), {"keyword": "债基"})
    assert hit["chosen"] == 1 and hit["picked"]["name"] == "安心债基 A"


def test_fuzzy_subscription_match_requires_high_similarity():
    """ratio 0.50 会把「爱奇艺体育」配到「爱奇艺黄金会员」—— 取消订阅不可逆。"""
    items = [{"id": 1, "merchant": "腾讯视频VIP"}, {"id": 2, "merchant": "爱奇艺黄金会员"}]
    miss = gateway._post("sub.list", {"items": [dict(x) for x in items]},
                         {"keyword": "爱奇艺体育"})
    assert miss.get("not_found") is True and miss.get("chosen") is None

    hit = gateway._post("sub.list", {"items": [dict(x) for x in items]}, {"keyword": "腾讯视频"})
    assert hit["chosen"] == 1


def test_redeem_unknown_product_end_to_end_does_not_move_money(client):
    """端到端：赎回一个不存在的产品，不能产生任何赎回动作。"""
    r = client.post("/api/chat", json={"text": "赎回黄金ETF联接"}).json()
    assert r["card"]["type"] == "notice" or "没找到" in r["reply"]
