"""审计落库前的 PII 脱敏。

银行场景下审计表不该出现明文姓名 / 卡号 / 账号 / 用户原话：
- 脱敏后的正文进 `audit_logs.payload`，并参与哈希 → 链只覆盖脱敏正文，
  原文变更不影响链的完整性校验；
- 原文摘要进 `pii_vault`（独立表），只存 HMAC 摘要不可逆，
  便于单独授权、单独设定保留期、单独销毁（crypto-shredding）。

设计取舍：默认不可逆摘要而不是可解密存储。
只用标准库时没有可靠的对称加密原语（`hashlib`/`hmac` 单向，`ssl` 不给 AES），
与其自己拼一个 AES-ECB 出来，不如把"可解密"做成注入式接口留给行内 KMS。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Any

from backend.core.config import DATA_DIR

# 卡号/账号：16-19 位连续数字
_CARD_RE = re.compile(r"\b\d{16,19}\b")
# 手机号
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
# 身份证：18 位（末位可能是 X）
_ID_RE = re.compile(r"\b\d{17}[\dXx]\b")

# 这些 key 的值按"整体替换"处理，不做局部打码——避免拼出可反推的残片
NAME_KEYS = {"name", "payee_name", "who", "payee", "customer_name", "real_name"}
TEXT_KEYS = {"text", "memo", "reason", "reply", "query", "label"}
ACCOUNT_KEYS = {"account", "account_no", "account_id_no", "card_no", "phone", "id_card",
                "payee_account", "account_last4", "payee_last4"}

_KEY_ID = os.getenv("YINSHU_PII_KEY_ID", "local-v1")
_MASTER = os.getenv("YINSHU_PII_KEY", "").encode() or b"yinshu-local-pii-master-key"
RETENTION_DAYS = int(os.getenv("YINSHU_PII_RETENTION_DAYS", "90"))


def mask_name(v: str) -> str:
    """张明 → 张*；欧阳明月 → 欧**。保留姓，避免审计完全不可读。"""
    if not v:
        return v
    return v[0] + "*" * max(1, len(v) - 1)


def mask_account(v: str) -> str:
    s = str(v)
    return "****" + s[-4:] if len(s) >= 4 else "****"


def mask_text(v: str) -> str:
    """用户原话：只留长度，不留内容。审计要的是"说了多长的一句话"，不是"说了什么"。"""
    return f"<redacted len={len(v)}>"


def scrub_string(v: str) -> str:
    """兜底：任何字符串里的长数字串都打码，避免通过 memo 等自由字段漏号。"""
    out = _ID_RE.sub(lambda m: "****" + m.group()[-4:], v)
    out = _CARD_RE.sub(lambda m: "****" + m.group()[-4:], out)
    out = _PHONE_RE.sub(lambda m: m.group()[:3] + "****" + m.group()[-4:], out)
    return out


def _scrub(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, key) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    if key in NAME_KEYS:
        return mask_name(s)
    if key in TEXT_KEYS:
        return mask_text(s)
    if key in ACCOUNT_KEYS:
        return mask_account(s)
    return scrub_string(s)


def redact(payload: dict) -> tuple[dict, str | None]:
    """返回 (脱敏后的 payload, pii_ref)。没有需要脱敏的字段时 pii_ref 为 None。"""
    if not isinstance(payload, dict):
        return {"value": scrub_string(str(payload))}, None

    redacted = _scrub(payload)
    if json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str) == \
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str):
        return redacted, None          # 没有敏感字段，不写 pii_vault

    ref = uuid.uuid4().hex
    return redacted, ref


def digest(payload: dict) -> str:
    """原文的 HMAC 摘要。同一原文恒定，可用于"是不是同一条"的比对，但不可还原。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hmac.new(_MASTER, body.encode("utf-8"), hashlib.sha256).hexdigest()


def key_id() -> str:
    return _KEY_ID


def retention_seconds() -> int:
    return RETENTION_DAYS * 86400


def vault_path() -> str:
    """给运维文档用的落库位置说明。"""
    return str(DATA_DIR)
