"""注入检测（L1 输入层）。

只做一件事：给输入打上 untrusted / suspicious 标签，供上层决定是否降级。
不作为唯一防线——真正的兜底在权限闸门（L4）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

RULES: list[tuple[str, str, int]] = [
    (r"忽略.{0,8}(之前|以上|前面|所有|此前).{0,6}(指令|规则|提示|设定|要求)", "忽略指令", 3),
    (r"(你现在是|从现在开始你|你不再是|假装你是|扮演一个|你是一个没有)", "角色覆盖", 3),
    (r"(系统提示词|system\s*prompt|提示词|你的设定|初始指令)", "探测提示词", 2),
    (r"(跳过|绕过|免去|不用|不需要).{0,6}(风控|验证|确认|密码|人脸|限额|审核|双录)", "绕过风控", 3),
    (r"(我是|作为).{0,8}(行长|管理员|客服|老板|后台|工作人员|风控)", "身份冒充", 3),
    (r"把(全部|所有|我).{0,4}(余额|钱|资金|存款)", "转移全部资金", 3),
    (r"(转账|汇款|打款).{0,6}\d{6,}", "指向具体账号", 2),
    (r"</?\s*(script|memo|system|user|instruction|tool)\b", "标签注入", 3),
    (r"(base64|atob|解码|decode|rot13)", "编码规避", 2),
    (r"(不要|别).{0,4}(告诉|通知|提示).{0,4}(用户|本人|他|她)", "隐瞒行为", 3),
    (r"(验证码|密码|短信码).{0,6}(发|给|告诉|报)", "索取凭据", 3),
    (r"(余额|资金|存款|所有的钱|全部的钱).{0,8}(清零|清空|转走|转出|划走)", "清空余额", 3),
    (r"悄(悄|悄地|无声息).{0,10}(转|划|扣|取|办)", "隐蔽操作", 3),
    (r"\b(ignore|disregard|forget)\b.{0,32}\b(previous|above|all)\b", "ignore-instruction", 3),
    (r"(sudo|rm\s+-rf|/etc/passwd|drop\s+table)", "系统指令", 3),
]

# 同形字：西里尔/希腊字母冒充拉丁字母
HOMOGLYPH = re.compile(r"[\u0400-\u04FF\u0370-\u03FF]")


@dataclass
class ScanResult:
    flagged: bool = False
    level: str = "clean"          # clean / suspicious / high
    hits: list[str] = field(default_factory=list)
    weight: int = 0
    cleaned: str = ""

    def to_dict(self) -> dict:
        return {"flagged": self.flagged, "level": self.level, "hits": self.hits, "weight": self.weight}


def _normalize(text: str) -> str:
    # 全角转半角 + 去掉不可见字符，避免"忽 略 之 前"这类规避
    t = unicodedata.normalize("NFKC", text)
    return INVISIBLE.sub("", t)


def scan(text: str) -> ScanResult:
    raw = text or ""
    cleaned = _normalize(raw)
    r = ScanResult(cleaned=cleaned)

    if INVISIBLE.search(raw):
        r.hits.append("零宽/双向控制字符")
        r.weight += 3
    if HOMOGLYPH.search(cleaned):
        r.hits.append("同形字混淆")
        r.weight += 2

    flat = re.sub(r"\s+", "", cleaned)
    for pat, label, w in RULES:
        if re.search(pat, flat, flags=re.I) or re.search(pat, cleaned, flags=re.I):
            r.hits.append(label)
            r.weight += w

    r.flagged = r.weight >= 2
    # 只有"多个强信号同时命中"才硬拦截；
    # 单个中等信号留给权限闸门降级——这才是纵深防御该有的样子。
    r.level = "high" if r.weight >= 5 else ("suspicious" if r.flagged else "clean")
    return r


def scan_untrusted_data(fields: dict) -> ScanResult:
    """间接注入：备注、商户名、产品名这类"会进入上下文的数据"也要过一遍。"""
    merged = " ".join(str(v) for v in fields.values() if v)
    return scan(merged)
