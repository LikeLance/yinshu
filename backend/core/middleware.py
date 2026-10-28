"""HTTP 中间件：请求上下文 / 访问日志 / 指标 / 限流 / 请求体大小。

顺序很重要（外到内）：
  RequestContext → RateLimit → BodyLimit → 业务路由
先给请求打上 trace id，再判断要不要限流，然后才校验体积——这样被限流的请求也能记到日志里。
"""
from __future__ import annotations

import hmac
import re
import time
import uuid
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.core import config, metrics
from backend.core.logging import get

log = get("http")

_SKIP_LOG = {"/healthz", "/readyz", "/metrics", "/favicon.ico"}

# 长度 ≥6 的十六进制或纯数字段视为"标识符"（trace_id/uuid/自增 id），归一成一个固定标签。
_ID_SEG = re.compile(r"(?:[0-9a-fA-F]{6,}|\d+)")


def _client_key(request: Request) -> str:
    """限流键：只有在明确配置了可信反代时才采信 X-Forwarded-For。

    默认信任 XFF 等于把限流做成装饰品 —— 换个请求头就是新身份，
    顺便把审计里的来源 IP 也一起污染了。
    """
    if config.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _route_label(request: Request) -> str:
    """指标标签：优先用路由模板，避免 /api/audit/<uuid> 把序列撑爆。

    中间件跑在路由匹配之前，`scope["route"]` 往往是空的，于是回落到原始路径 ——
    这时一个随机 id 就是一条新时间序列，Prometheus 的基数会随请求量无界增长。
    回落时按"像不像 id"做归一化。
    """
    route = request.scope.get("route")
    tpl = getattr(route, "path", None)
    if tpl:
        return tpl
    parts = ["{id}" if _ID_SEG.fullmatch(seg) else seg for seg in request.url.path.split("/")]
    return ("/".join(parts) or "/")[:120]


class RequestContext(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - t0) * 1000
            metrics.inc("yinshu_http_requests_total", method=request.method,
                        path=_route_label(request), status="500")
            log.exception("unhandled", request_id=rid, method=request.method,
                          path=request.url.path, ms=round(elapsed, 1))
            raise
        elapsed = (time.perf_counter() - t0) * 1000
        response.headers["x-request-id"] = rid
        response.headers["x-response-time-ms"] = f"{elapsed:.1f}"

        path = _route_label(request)
        metrics.inc("yinshu_http_requests_total", method=request.method,
                    path=path, status=str(response.status_code))
        metrics.observe("yinshu_http_request", elapsed / 1000)

        if request.url.path not in _SKIP_LOG:
            log.info("request", request_id=rid, method=request.method,
                     path=request.url.path, status=response.status_code,
                     ms=round(elapsed, 1), ip=_client_key(request))
        return response


class RateLimit(BaseHTTPMiddleware):
    """令牌桶：每来源每分钟 RPM 次，平滑补充。RPM<=0 表示关闭（演示档默认关闭）。"""

    def __init__(self, app, rpm: int | None = None) -> None:
        super().__init__(app)
        self.rpm = config.RATE_LIMIT_RPM if rpm is None else rpm
        self.capacity = max(1, self.rpm)
        self.rate = self.capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = defaultdict(lambda: (self.capacity, time.time()))
        self._ops = 0

    MAX_BUCKETS = 20000          # 桶的硬上限，防止被扫 IP 打爆内存
    IDLE_SECONDS = 600.0         # 超过这个时长没请求的桶直接回收

    def _evict(self, now: float) -> None:
        """回收冷桶。

        原来 `_buckets` 只增不减：来源越散字典越大，长期跑就是慢性内存泄漏
        （而且 defaultdict 让"读"也会创建条目）。
        """
        self._ops += 1
        if len(self._buckets) <= self.MAX_BUCKETS or self._ops % 256:
            return
        cut = now - self.IDLE_SECONDS
        for k in [k for k, (_t, ts) in self._buckets.items() if ts < cut]:
            self._buckets.pop(k, None)
        if len(self._buckets) > self.MAX_BUCKETS:
            order = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
            for k, _ in order[: len(order) // 2]:
                self._buckets.pop(k, None)

    def _take(self, key: str) -> tuple[bool, float]:
        tokens, last = self._buckets[key]
        now = time.time()
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False, (1 - tokens) / self.rate
        self._buckets[key] = (tokens - 1, now)
        return True, 0.0

    async def dispatch(self, request: Request, call_next) -> Response:
        if self.rpm <= 0 or request.url.path in ("/healthz", "/readyz", "/metrics"):
            return await call_next(request)
        ok, wait = self._take(_client_key(request))
        if not ok:
            # 用路由模板而不是原始路径：被限流的往往是扫描器，路径千奇百怪，
            # 原始路径当标签等于让攻击者决定你的时序基数。
            metrics.inc("yinshu_rate_limited_total", path=_route_label(request))
            log.warning("rate_limited", path=request.url.path, ip=_client_key(request))
            return JSONResponse(
                {"ok": False, "error": {"code": "RATE_LIMITED",
                                        "message": "请求过于频繁，请稍后再试"}},
                status_code=429, headers={"Retry-After": f"{max(1, int(wait + 0.5))}"})
        return await call_next(request)


def _too_large(limit: int) -> JSONResponse:
    metrics.inc("yinshu_rejected_total", reason="body_too_large")
    return JSONResponse(
        {"ok": False, "error": {"code": "BODY_TOO_LARGE",
                                "message": f"请求体超过 {limit} 字节"}},
        status_code=413)


class BodyLimit(BaseHTTPMiddleware):
    """请求体体积上限。

    只信 `Content-Length` 是能被绕过的：分块传输（chunked）根本不带这个头，
    攻击者可以一边发一边无限灌。所以长度缺失时**边读边数**，超限立刻断。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in ("POST", "PUT", "PATCH"):
            return await call_next(request)

        limit = config.MAX_BODY_BYTES
        raw_len = request.headers.get("content-length")
        try:
            length = int(raw_len) if raw_len is not None else None
        except ValueError:
            length = None

        if length is not None and length > limit:
            return _too_large(limit)

        if length is None:
            total, chunks = 0, []
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    return _too_large(limit)
                chunks.append(chunk)
            body = b"".join(chunks)

            async def _replay():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = _replay          # noqa: SLF001  下游还要读一次

        return await call_next(request)


class VersionAlias(BaseHTTPMiddleware):
    """把 `/api/v1/xxx` 在进入路由前改写成 `/api/xxx`。

    为什么不在 app 上挂两遍路由：`include_router` 的 `include_in_schema=False`
    在 FastAPI 0.162 下不生效，双挂载会让 OpenAPI 里每个接口重复一份、
    并刷一堆 "Duplicate Operation ID" 警告。路由只注册一份、在边缘做别名，
    既保持了 `/api/v1` 的兼容性，文档也干净。
    """

    FROM = "/api/v1/"
    TO = "/api/"

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.scope.get("path", "")
        if path.startswith(self.FROM):
            request.scope["path"] = self.TO + path[len(self.FROM):]
        return await call_next(request)


class ApiKeyGate(BaseHTTPMiddleware):
    """可选接入鉴权：配了 YINSHU_API_KEYS 才生效。

    真上线时这一层通常由网关/mTLS 承担；放在这里是为了让项目自带最小可用的门禁，
    而不是等到接入行内统一认证才发现接口是裸的。
    """

    def __init__(self, app, keys: list[str] | None = None) -> None:
        super().__init__(app)
        self.keys = set(config.API_KEYS if keys is None else keys)

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.keys:
            return await call_next(request)
        path = request.url.path
        if path in config.AUTH_EXEMPT or path.startswith(("/docs", "/redoc")):
            return await call_next(request)
        supplied = request.headers.get("x-api-key") or ""
        # 常量时间比较：`in set` 会按前缀提前返回，理论上能把 key 一个字符一个字符试出来
        if supplied and any(hmac.compare_digest(supplied, k) for k in self.keys):
            return await call_next(request)
        metrics.inc("yinshu_rejected_total", reason="unauthorized")
        log.warning("unauthorized", path=path, ip=_client_key(request))
        return JSONResponse(
            {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "缺少或错误的 X-API-Key"}},
            status_code=401, headers={"WWW-Authenticate": "ApiKey"})
