"""统一错误码。业务错误提供用户可读信息，不暴露内部检测规则。"""
from __future__ import annotations


class YinshuError(Exception):
    code = "INTERNAL"
    user_msg = "出了点小问题，请稍后再试。"

    def __init__(self, detail: str = "", user_msg: str | None = None):
        self.detail = detail
        if user_msg:
            self.user_msg = user_msg
        super().__init__(f"[{self.code}] {detail or self.user_msg}")


class NluLowConf(YinshuError):
    code = "NLU_LOW_CONF"
    user_msg = "没太听明白，您是想办下面哪件事？"


class SlotMissing(YinshuError):
    code = "SLOT_MISSING"

    def __init__(self, slots: list[str], user_msg: str = ""):
        self.slots = slots
        super().__init__(",".join(slots), user_msg or "还差一点信息，能补充一下吗？")


class PayeeAmbiguous(YinshuError):
    code = "PAYEE_AMBIGUOUS"
    user_msg = "找到多位可能的收款人，请选择一位。"


class Ambiguous(YinshuError):
    """泛化的"多候选"：收款人重名、订阅商户多次命中、产品名不唯一。"""
    code = "AMBIGUOUS"

    def __init__(self, kind: str, options: list[dict], user_msg: str = ""):
        self.kind = kind
        self.options = options
        super().__init__(kind, user_msg or "找到多个可能的匹配项，请选择一项。")


class ToolFailed(YinshuError):
    code = "TOOL_FAILED"
    user_msg = "银行业务系统暂时繁忙，请稍后再试。"


class LimitExceeded(YinshuError):
    code = "LIMIT_EXCEEDED"


class InsufficientFunds(YinshuError):
    code = "INSUFFICIENT_FUNDS"


class PolicyDeny(YinshuError):
    code = "POLICY_DENY"


class PolicyHandoff(YinshuError):
    code = "POLICY_HANDOFF"


class InjectionBlocked(YinshuError):
    code = "INJECTION_BLOCKED"
    user_msg = "这段请求超出了我能代办的范围，换个说法试试？"


ERROR_CODES = {
    "NLU_LOW_CONF": "意图置信度不足",
    "SLOT_MISSING": "缺少必填槽位",
    "PAYEE_AMBIGUOUS": "收款人同名多人",
    "LIMIT_EXCEEDED": "超出限额",
    "INSUFFICIENT_FUNDS": "余额不足",
    "POLICY_DENY": "策略拒绝",
    "POLICY_HANDOFF": "转人工",
    "SYSTEM_BUSY": "下游繁忙",
    "INJECTION_BLOCKED": "注入拦截",
    "TOOL_FAILED": "工具调用失败",
    "INTERNAL": "内部错误",
}
