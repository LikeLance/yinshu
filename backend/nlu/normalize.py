"""输入归一化：让"人怎么写"和"规则怎么认"之间少一层噪音。

规则 NLU 天生脆：同一个意思多一个 emoji、多一个空格、全角数字、
或者粘贴时带的零宽字符，就会从"命中"变成"没识别"。这些都不是用户的问题，
是预处理没做。所以在 `nlu.parse()` 的最前面统一过一遍。

只做**无损**归一化（不改变语义）：
- NFKC：全角数字/字母/标点 → 半角（５００ → 500）；
- 去掉零宽与双向控制字符（复制粘贴的常见污染）；
- 去掉 emoji 与变体选择符；
- 空白折叠。

**不做**繁简转换：这需要一张词表，做一半会比不做更危险（把正常词改坏）。
繁简混输登记为已知缺口，见 docs/review-2026-10-27.md。
"""
from __future__ import annotations

import re
import unicodedata

_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# emoji / 符号区：装饰性图案、国旗、箭头、变体选择符、零宽连接符、键帽
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200D\u20E3]"
)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _INVISIBLE.sub("", t)
    t = _EMOJI.sub(" ", t)
    t = _SPACE.sub(" ", t)
    return t.strip()
