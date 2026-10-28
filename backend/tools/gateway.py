"""工具网关：编排层访问外界（银行核心 / 三方）的唯一出口。

三件事：
1. 参数校验（白名单 + 必填 + 类型 + 拒绝多余字段）；
2. 幂等键与重试；
3. 结果后处理（例如把"收款人候选"收窄为"确定的一个"）。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from backend.core.config import BANK_BASE
from backend.core.errors import (InsufficientFunds, LimitExceeded, ToolFailed, YinshuError)
from backend.tools import registry

TIMEOUT = 6.0
MAX_RETRY = 2

# 共享连接池：一次对话常常要连打 2~4 个下游接口，每次新建 TCP 在 Windows 上要 20~30ms
_CLIENT: httpx.Client | None = None


def client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(
            timeout=TIMEOUT,
            trust_env=False,          # 本机/内网调用不吃系统代理
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=60,
                                keepalive_expiry=30.0),
            headers={"User-Agent": "yinshu-gateway/1.0"},
        )
    return _CLIENT


def close() -> None:
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        _CLIENT.close()
    _CLIENT = None

_CODE_MAP = {
    "INSUFFICIENT_FUNDS": InsufficientFunds,
    "LIMIT_EXCEEDED": LimitExceeded,
    "PAYEE_BLACKLISTED": YinshuError,
    "SYSTEM_BUSY": ToolFailed,
}


class ToolArgError(YinshuError):
    code = "TOOL_ARG_ERROR"
    user_msg = "请求参数不完整。"


class PayeeNotFound(YinshuError):
    code = "PAYEE_NOT_FOUND"
    user_msg = "通讯录里没找到这个人，试试加上姓氏或手机号后四位？"


class PayeeAmbiguous(YinshuError):
    code = "PAYEE_AMBIGUOUS"
    user_msg = "找到多位可能的收款人，请选择一位。"


def _coerce(k: str, t: type, v: Any) -> Any:
    if v is None:
        return None
    if t is int:
        return int(v)
    if t is bool:
        return bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes")
    if t is str:
        if isinstance(v, Decimal):
            return f"{v:.2f}"
        return str(v)
    return v


def validate(spec: registry.ToolSpec, args: dict) -> dict:
    unknown = set(args) - set(spec.args)
    if unknown:
        raise ToolArgError(f"{spec.name} 不接受参数：{','.join(sorted(unknown))}")
    out: dict = {}
    for k, t in spec.args.items():
        v = args.get(k, spec.defaults.get(k))
        if v is None:
            if k in spec.required:
                raise ToolArgError(f"{spec.name} 缺少必填参数 {k}")
            continue
        out[k] = _coerce(k, t, v)
    for k in spec.required:
        if out.get(k) in (None, ""):
            raise ToolArgError(f"{spec.name} 缺少必填参数 {k}")
    return out


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        code = (detail or {}).get("code") if isinstance(detail, dict) else None
        msg = (detail or {}).get("message") if isinstance(detail, dict) else str(detail)
    except Exception:
        code, msg = None, resp.text[:120]
    exc = _CODE_MAP.get(code or "", YinshuError)
    if exc is YinshuError:
        raise YinshuError(f"{code or resp.status_code}: {msg}", user_msg=msg or "银行系统处理失败")
    raise exc(f"{code}: {msg}", user_msg=msg or "银行系统处理失败")


def _hdr_safe(v: str) -> str:
    """幂等键要写进 HTTP 头，必须是 ASCII。

    trace_id 可能来自客户端的 X-Request-Id（可伪造），直接拼进头会抛
    UnicodeEncodeError → 整个请求 500。这里做一次无损降级：非 ASCII 替换掉。
    """
    return str(v).encode("ascii", "replace").decode("ascii")


def _idem_key(name: str, step_id: str, principal: Any, idem_seed: str | None,
              trace_id: str) -> str:
    """确定性幂等键。

    原实现是 `f"{trace_id}-{step_id}"`，而 trace_id 每个请求新生成 →
    客户端重试就换了 key → 下游判重落空 → 重复转账。
    seed 的优先级：显式 idem_seed（确认态用 confirm_id、AUTO 用请求级幂等键）
    > 请求上下文里的 seed > trace_id（兜底）。
    uid 与 tool 进 key，保证不同用户/不同动作不会撞。
    """
    from backend.core import reqctx

    uid = int(getattr(principal, "uid", 0) or 0)
    seed = _hdr_safe(idem_seed or reqctx.idem_seed() or trace_id or "t")
    return f"yinshu:v2:{uid}:{name}:{seed}:{_hdr_safe(step_id or name)}"


def call(name: str, args: dict, trace_id: str = "", step_id: str = "",
         principal: Any = None, idem_seed: str | None = None) -> dict:
    from backend.core import reqctx

    principal = principal if principal is not None else reqctx.principal()
    spec = registry.get(name)
    a = validate(spec, args)
    local = {k: a.pop(k) for k in spec.local if k in a}

    path = spec.path
    for k in list(a):
        if "{" + k + "}" in path:
            path = path.replace("{" + k + "}", str(a.pop(k)))
    url = BANK_BASE + path

    headers: dict[str, str] = {}
    if spec.idem:
        headers["Idempotency-Key"] = _idem_key(name, step_id, principal, idem_seed, trace_id)
    # 主体透传：真实银行侧要据此做账户/权限校验，不能只靠我们这边自说自话
    if principal is not None:
        if getattr(principal, "uid", 0):
            headers["X-User-Id"] = str(principal.uid)
        if getattr(principal, "device_id", ""):
            headers["X-Device-Id"] = principal.device_id
        if getattr(principal, "channel", ""):
            headers["X-Channel"] = principal.channel
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    last_err: Exception | None = None
    for attempt in range(MAX_RETRY + 1):
        t0 = time.time()
        try:
            c = client()
            if spec.method == "GET":
                # 请求头走 GET 也要带上：X-User-Id 是核心系统做数据隔离的依据，
                # 漏了它所有读请求都会退化成"用户 1"。
                r = c.get(url, params={k: v for k, v in a.items() if v is not None},
                          headers=headers)
            elif spec.method == "POST":
                r = c.post(url, json=a, headers=headers)
            elif spec.method == "PATCH":
                r = c.patch(url, json=a, headers=headers)
            else:
                raise ToolArgError(f"不支持的方法 {spec.method}")
            if r.status_code >= 500:
                raise ToolFailed(f"{name} -> {r.status_code}")
            _raise_for(r)
            data = r.json()
            data["_meta"] = {"tool": name, "latency_ms": int((time.time() - t0) * 1000),
                             "attempt": attempt + 1}
            return _post(name, data, local)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_err = e
        except ToolFailed as e:
            last_err = e
        if attempt < MAX_RETRY:
            time.sleep(0.2 * (attempt + 1))
    raise ToolFailed(f"{name} 重试失败：{last_err}")


def _pick(items: list[dict], key: str, needle: str, data: dict) -> dict:
    """在若干候选里收敛成一个；收敛不了就标记 ambiguous，交给澄清卡片。

    匹配顺序：去掉口语尾巴 → 精确包含 → 编辑距离（"腾讯视频订阅" 要能对到 "腾讯视频VIP"）。
    """
    import difflib

    raw = (needle or "").strip()
    for suffix in ("自动续费", "自动扣款", "这个订阅", "的订阅", "订阅", "会员", "服务", "扣款"):
        if raw.endswith(suffix) and len(raw) > len(suffix) + 1:
            raw = raw[: -len(suffix)]
            break

    pool = items
    hit = [x for x in pool if raw and raw in str(x.get(key, ""))]
    if not hit and len(raw) >= 2:
        scored = []
        for x in pool:
            r = difflib.SequenceMatcher(None, raw, str(x.get(key, ""))).ratio()
            # 门槛 0.7（原 0.5）：这个函数服务于"取消订阅/挂失"这类不可逆动作，
            # 0.5 会让「爱奇艺体育」配上「爱奇艺黄金会员」（ratio 恰好 0.50），
            # 用户取消一个自己没有的服务，系统把另一个退了。
            if r >= 0.7:
                scored.append((r, x))
        scored.sort(key=lambda t: -t[0])
        hit = [x for _, x in scored]

    if len(hit) == 1:
        data["chosen"] = hit[0].get("id")
        data["picked"] = hit[0]
    elif len(hit) > 1:
        data["chosen"] = None
        data["ambiguous"] = True
        data["options"] = hit
    else:
        data["chosen"] = None
        data["not_found"] = True
    return data


def _post(name: str, data: dict, local: dict | None = None) -> dict:
    """工具结果后处理：把"一堆候选"收敛成"确定的一个"。"""
    local = local or {}

    if name == "payee.search":
        cands = data.get("candidates") or []
        if not cands:
            raise PayeeNotFound(data.get("query", ""))
        top = cands[0]
        tied = [c for c in cands if abs(c["score"] - top["score"]) < 1e-6]
        # 只看"关系是否不同"是不够的：两个同名同关系的联系人（同班同学、两个同事）
        # 会被静默取第一个，而产品对外的承诺是"绝不猜"。
        # 只要并列，就出候选卡让用户自己点。
        if len(tied) > 1:
            data["chosen"] = None
            data["ambiguous"] = True
            data["options"] = tied
            return data
        data["chosen"] = top["id"]
        data["ambiguous"] = False
        data["payee"] = top
        return data

    if name == "sub.list":
        items = data.get("items") or []
        kw = local.get("keyword")
        if kw:
            return _pick(items, "merchant", kw, data)
        return data

    if name == "card.list":
        items = data.get("cards") or []
        last4 = local.get("last4")
        if last4:
            return _pick(items, "last4", last4, data)
        if len(items) == 1:
            data["chosen"] = items[0]["id"]
        elif items:
            data["chosen"] = items[0]["id"]
            data["picked"] = items[0]
        return data

    if name == "wealth.products":
        items = data.get("products") or []
        kw = (local.get("keyword") or "").strip()
        if kw:
            import difflib

            hit = [p for p in items if kw in p["name"]]
            if not hit:
                scored = [(difflib.SequenceMatcher(None, kw, p["name"]).ratio(), p) for p in items]
                scored = [(r, p) for r, p in scored if r >= 0.4]
                scored.sort(key=lambda t: -t[0])
                hit = [p for _, p in scored]
            if len(hit) == 1:
                data["chosen"] = hit[0]["id"]
                data["picked"] = hit[0]
            elif len(hit) > 1:
                # 取相似度最高的一个（理财语义上"最接近"即可，不必打断用户）
                data["chosen"] = hit[0]["id"]
                data["picked"] = hit[0]
                data["ambiguous"] = False
            else:
                data["chosen"] = None
                data["not_found"] = True
        return data

    if name == "wealth.holdings":
        items = data.get("holdings") or []
        kw = (local.get("keyword") or "").strip()
        hit = [h for h in items if kw and kw in (h.get("name") or "")]
        if len(hit) == 1:
            data["chosen"] = hit[0]["id"]
            data["picked"] = hit[0]
        elif len(hit) > 1:
            data["chosen"] = None
            data["ambiguous"] = True
            data["options"] = hit
        elif not kw:
            # 没说赎回哪支 → 出候选让用户选，不替他挑
            data["chosen"] = None
            data["ambiguous"] = bool(items)
            data["options"] = items
            data["not_found"] = not items
        else:
            # 关键词对不上任何一个持仓。原来这里是 `chosen = items[0]`，
            # 等于"用户说要赎回 A，系统默默卖掉 B"——赎回不可逆，这是资金事故级的猜。
            data["chosen"] = None
            data["not_found"] = True
        return data

    return data
