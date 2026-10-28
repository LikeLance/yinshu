"""槽位解析：中文口语 → 结构化字段。

三路策略（对应方案 5.1）：
- 金额：中文数字优先，其次阿拉伯数字，最后正则兜底；解析时间/手机号前先"打码"，避免误判；
- 时间：相对时间（今天/明天/下周三）+ 周期（每月1号/每周一）+ 时刻（下午3点）；
- 人名：只抽原文，不做猜测，消歧交给通讯录检索。

这些函数全部是纯函数，方便单测。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from backend.core import clock, cnnum

CN_DIGITS = cnnum.DIGITS
CN_UNITS = cnnum.UNITS
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_WEEK_LABEL = "一二三四五六日"


def cn2num(s: str) -> int | None:
    """中文数字转整数：五百→500；两千五→2500；十五→15；一万二→12000；一百零五→105。

    实现在 `backend/core/cnnum.py`，这里只保留旧名字 ——
    这个算法曾经在 NLU 和幻觉校验各存一份，然后只修了一处，
    结果「转一百零五块」被 NLU 解析成 150 元。金额逻辑不能有两个真相。
    """
    return cnnum.to_int(s)


# ---------------- 打码：把不该被当成金额/时间的东西遮住 ----------------

MASK_PATTERNS = [
    r"1[3-9]\d{9}",                       # 手机号
    r"1[3-9]\d\*{2,}\d{4}",               # 脱敏手机号
    r"\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}",  # 卡号
    r"尾号\s*\d{3,4}",
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]",    # 10月26日
    r"每(?:个)?(?:月|周|星期|礼拜)\s*\d{1,2}\s*[号日]",  # 每月1号
    r"\d{1,2}\s*[号日]",                   # 1号
    r"(?:上午|早上|中午|下午|晚上|夜里|凌晨)?\s*\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分?)?",  # 下午3点
    r"(?:下|这|本|下下)?(?:个)?(?:周|星期|礼拜)[一二三四五六日天]",  # 下周三
    r"(?:上午|早上|中午|下午|晚上|夜里|凌晨)",
]


def mask_noise(text: str) -> str:
    out = text
    for p in MASK_PATTERNS:
        out = re.sub(p, lambda m: "□" * len(m.group()), out)
    return out


# ---------------- 金额 ----------------

AMOUNT_CN = re.compile(r"([零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万]{1,8})\s*(?:元|块|块钱|圆)")
AMOUNT_UNIT = re.compile(r"(?<![\d.])(\d{1,7}(?:\.\d{1,2})?)\s*(万元|万|元|块|块钱|圆)")
AMOUNT_BARE = re.compile(r"(?<![\d.□])(\d{1,7}(?:\.\d{1,2})?)(?![\d.□])")
# 无单位的中文金额：只认紧跟在动作词后面、且后面不再接汉字的（"转一千二，"）
AMOUNT_CN_VERB = re.compile(r"(?:转|打|汇|付|给)\s*([零〇一二两三四五六七八九十百千万]{2,6})(?![\u4e00-\u9fa5])")


def parse_amount(text: str, masked: str | None = None) -> Decimal | None:
    m = masked if masked is not None else mask_noise(text)
    mm = AMOUNT_CN.search(m)
    if mm:
        v = cn2num(mm.group(1))
        if v:
            return Decimal(v)
    mm = AMOUNT_UNIT.search(m)
    if mm:
        v = Decimal(mm.group(1))
        return v * 10000 if "万" in mm.group(2) else v
    mm = AMOUNT_BARE.search(m)
    if mm:
        return Decimal(mm.group(1))
    mm = AMOUNT_CN_VERB.search(m)
    if mm:
        v = cn2num(mm.group(1))
        if v:
            return Decimal(v)
    return None


# ---------------- 人数（AA 用） ----------------

def parse_people(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*(?:个)?人", text)
    if m:
        return int(m.group(1))
    m = re.search(r"([一二两三四五六七八九十])\s*(?:个)?人", text)
    if m:
        return cn2num(m.group(1))
    return None


# ---------------- 时间 ----------------

DATE_REL = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
MONTHLY = re.compile(r"每(?:个)?月\s*(\d{1,2})\s*[号日]")
WEEKLY = re.compile(r"每(?:个)?(?:周|星期|礼拜)([一二三四五六日天])")
DAILY = re.compile(r"每天|每日")
WEEKDAY_RE = re.compile(r"(下下|下|这|本)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天])")
MONTHDAY_RE = re.compile(r"(下个?月|这个?月)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
TIME_RE = re.compile(r"(上午|早上|中午|下午|晚上|夜里|凌晨)?\s*(\d{1,2})\s*(?:[点时]|\:)(?:\s*(\d{1,2})\s*分?)?")
HOUR_WORD = {"上午": 0, "早上": 0, "中午": 0, "下午": 12, "晚上": 12, "夜里": 12, "凌晨": 0}


def _last_day(y: int, m: int) -> int:
    return [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]


def _date_in_month(y: int, m: int, day: int) -> date:
    """该月的第 day 天；超出本月天数就取月末（「每月31号」在 2 月落到 28/29 号）。"""
    return date(y, m, min(max(day, 1), _last_day(y, m)))


def _add_month(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    return _date_in_month(y, m, d.day)


def _label(d: date, hh: str | None = None) -> str:
    s = f"{d.month}月{d.day}日（周{CN_WEEK_LABEL[d.weekday()]}）"
    return s + (f" {hh}" if hh else "")


def parse_when(text: str, today: date | None = None) -> dict:
    """返回 {date, time, recurrence, label, explicit}。recurrence 形如 monthly:1 / weekly:0。"""
    today = today or clock.now().date()
    out: dict = {"date": None, "time": None, "recurrence": None, "label": None, "explicit": False}

    # 时刻
    tm = TIME_RE.search(text)
    if tm:
        prefix, h, mi = tm.group(1), int(tm.group(2)), int(tm.group(3) or 0)
        if prefix in ("下午", "晚上", "夜里") and h < 12:
            h += 12
        if prefix in ("上午", "早上", "凌晨") and h == 12:
            h = 0
        if 0 <= h <= 23:
            out["time"] = f"{h:02d}:{mi:02d}"

    # 周期
    mm = MONTHLY.search(text)
    if mm:
        day = min(max(int(mm.group(1)), 1), 31)
        # 首次执行日必须落在未来。原来用 today.replace(day=min(day, 28))：
        # 「每月31号」会被悄悄改成 28 号 —— 今天 29 号时首次执行日是**已经过去的 10-28**，
        # label 却写着「每月31号」，定时任务就按这个日期排下去了。
        first = _date_in_month(today.year, today.month, day)
        if first < today:
            nxt = _add_month(today.replace(day=1), 1)
            first = _date_in_month(nxt.year, nxt.month, day)
        hh = out["time"] or "09:00"
        out.update(date=first.isoformat(), time=hh, recurrence=f"monthly:{day}",
                   label=f"每月{day}号 {hh}", explicit=True)
        return out
    mw = WEEKLY.search(text)
    if mw:
        idx = WEEKDAY[mw.group(1)]
        delta = (idx - today.weekday()) % 7 or 7
        first = today + timedelta(days=delta)
        hh = out["time"] or "09:00"
        out.update(date=first.isoformat(), time=hh, recurrence=f"weekly:{idx}",
                   label=f"每周{CN_WEEK_LABEL[idx]} {hh}", explicit=True)
        return out
    if DAILY.search(text):
        hh = out["time"] or "09:00"
        out.update(date=(today + timedelta(days=1)).isoformat(), time=hh, recurrence="daily",
                   label=f"每天 {hh}", explicit=True)
        return out

    # 具体日期
    d: date | None = None
    for w, off in DATE_REL.items():
        if w in text:
            d = today + timedelta(days=off)
            break
    if d is None:
        md = MONTHDAY_RE.search(text)
        if md:
            y = today.year + (1 if md.group(1) and "下" in md.group(1) and today.month == 12 else 0)
            try:
                d = date(y, int(md.group(2)), int(md.group(3)))
            except ValueError:
                d = None
    if d is None:
        wd = WEEKDAY_RE.search(text)
        if wd and wd.group(2):
            idx = WEEKDAY[wd.group(2)]
            base = wd.group(1) or ""
            monday = today - timedelta(days=today.weekday())
            if base in ("下", "下下"):
                d = monday + timedelta(days=7 * (2 if base == "下下" else 1) + idx)
            else:  # 本周 / 未指明
                cand = monday + timedelta(days=idx)
                d = cand if cand >= today else cand + timedelta(days=7)
    if d is None:
        # 只说「26号 / 26日」：必须在**原文**上找。
        # 原实现在 mask_noise(text) 上匹配 `(\d{1,2})[号日]`，可 mask 已经把数字换成 □，
        # 这条分支永远不可达 —— 用户说了日期，系统当没说。
        for hit in re.finditer(r"(?<![\d])(\d{1,2})\s*[号日]", text):
            day = int(hit.group(1))
            if not 1 <= day <= 31:
                continue
            lead = text[max(0, hit.start() - 6):hit.start()]
            if "尾号" in lead:                 # 「尾号6621」不是日期
                continue
            base = _add_month(today.replace(day=1), 1) if "下个" in lead else today
            cand = _date_in_month(base.year, base.month, day)
            if cand < today:                   # 这个月的这天已经过了 → 顺延到下个月
                nxt = _add_month(base.replace(day=1), 1)
                cand = _date_in_month(nxt.year, nxt.month, day)
            d = cand
            break
    if d is not None:
        out.update(date=d.isoformat(), label=_label(d, out["time"]), explicit=True)
    return out


# ---------------- 账单周期 ----------------

def parse_period(text: str, today: date | None = None) -> dict:
    today = today or clock.now().date()
    first = today.replace(day=1)
    if any(k in text for k in ("上月", "上个月")):
        prev_last = first - timedelta(days=1)
        return {"from": prev_last.replace(day=1).isoformat(), "to": prev_last.isoformat(), "label": "上个月"}
    if any(k in text for k in ("近半年", "最近半年", "这半年", "半年")):
        return {"from": (today - timedelta(days=182)).isoformat(), "to": today.isoformat(), "label": "近半年"}
    if any(k in text for k in ("近三个月", "最近三个月", "三个月")):
        return {"from": (today - timedelta(days=90)).isoformat(), "to": today.isoformat(), "label": "近三个月"}
    if any(k in text for k in ("今年", "年度", "本年", "全年")):
        return {"from": today.replace(month=1, day=1).isoformat(), "to": today.isoformat(), "label": "今年至今"}
    if any(k in text for k in ("上周", "上个周")):
        monday = today - timedelta(days=today.weekday())
        s = monday - timedelta(days=7)
        return {"from": s.isoformat(), "to": (s + timedelta(days=6)).isoformat(), "label": "上周"}
    return {"from": first.isoformat(), "to": today.isoformat(), "label": "本月"}


# ---------------- 人名 / 备注 ----------------

PAYEE_PATTERNS = [
    r"给\s*(?:我)?\s*([\u4e00-\u9fa5A-Za-z]{1,4}?)\s*(?:转|汇|打|付)",
    r"(?:转给|转帐给|转账给|打给|汇给|付给)\s*([\u4e00-\u9fa5A-Za-z]{1,4})",
    r"向\s*([\u4e00-\u9fa5]{1,4})\s*(?:收|要|发起)",
    r"给\s*([\u4e00-\u9fa5A-Za-z]{1,4})\s*的",
    r"给\s*([\u4e00-\u9fa5A-Za-z]{1,4})\s*(?:转|打)?\s*\d",
    r"给\s*(?:我)?\s*([\u4e00-\u9fa5A-Za-z]{1,3})\s*[。！!]?$",   # 「…转500给我妈」
]
MEMO_RE = re.compile(r"(?:备注|附言|留言|写着|注明)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9]{1,20})")


def parse_payee(text: str) -> str | None:
    for p in PAYEE_PATTERNS:
        m = re.search(p, text)
        if m:
            v = m.group(1).strip()
            v = re.sub(r"^(我的|我)", "", v)
            if v and v not in ("我", "你", "他", "她", "钱"):
                return v
    return None


def parse_keyword_after(text: str, words: tuple[str, ...]) -> str | None:
    for w in words:
        i = text.find(w)
        if i >= 0:
            tail = text[i + len(w):]
            m = re.match(r"[\s:：]*([\u4e00-\u9fa5A-Za-z0-9]{2,12})", tail)
            if m:
                return clean_keyword(m.group(1))
    return None


KEYWORD_NOISE = re.compile(r"\d+(?:\.\d+)?\s*(?:万元|万|元|块钱|块|圆)?|[元的块了买把帮我它那这，。、！!？?：:\s]")


def clean_keyword(s: str) -> str:
    """把「1000块安心债基」「那个安心债基吧」洗成「安心债基」。"""
    out = KEYWORD_NOISE.sub("", s or "")
    for pre in ("申购", "买入", "买", "投", "赎回", "取消", "退订", "关掉", "停用", "订阅"):
        if out.startswith(pre):
            out = out[len(pre):]
    return out[:10]


def parse_memo(text: str) -> str | None:
    m = MEMO_RE.search(text)
    return m.group(1).strip() if m else None


def parse_card_last4(text: str) -> str | None:
    m = re.search(r"尾号\s*(\d{4})", text)
    return m.group(1) if m else None
