"""中文数字解析，供 NLU 与数字校验共用。

金额槽位和幻觉校验都需要把中文数字转换为整数。两处分别实现容易产生口径差异，
例如同一句“一百零五”在金额解析中得到 150、在校验中得到 105。
金额解析错误会直接影响交易参数，因此这里维护唯一实现。
"""
from __future__ import annotations

DIGITS: dict[str, int] = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1,
    "二": 2, "两": 2, "贰": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
UNITS: dict[str, int] = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
    "万": 10000,
    "亿": 100000000,
}
ZERO = {"零", "〇"}
# 给正则用的字符集（去重保序）
CHARS = "".join(dict.fromkeys("".join(DIGITS) + "".join(UNITS)))


def to_int(s: str) -> int | None:
    """中文数字 → 整数；无法解析返回 None。

    支持：
    - 常规：十五=15、五百=500、两百五=250、一万二=12000（口语省略末位单位）
    - 带零的真个位：一百零五=105、一千零八=1008（"零"后面是个位真值，不能按省略读）
    - 大写金额：壹仟贰佰=1200
    """
    if not s:
        return None
    total = 0
    section = 0
    num = 0
    last_unit = 0
    zero_before = False
    for ch in s:
        if ch in DIGITS:
            if ch in ZERO:
                zero_before = True
                num = 0
            else:
                num = DIGITS[ch]
            continue
        unit = UNITS.get(ch)
        if unit is None:
            return None
        last_unit = unit
        if unit >= 10000:                  # 万/亿自成一节
            section = (section + num) * unit
            total += section
            section = 0
        else:
            section += (num or 1) * unit   # "十五"里"十"前面没有数字，按 1 算
        num = 0
        zero_before = False
    # 口语省略："一百二"=120、"一千二"=1200、"一万二"=12000
    # 但"一百零五"=105 —— 有"零"就是真个位
    if num and not zero_before and 1 <= num <= 9 and last_unit >= 100:
        return total + section + num * (last_unit // 10)
    return total + section + num
