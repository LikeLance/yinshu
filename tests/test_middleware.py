"""中间件：限流、请求体上限、请求上下文头。

用最小 Starlette 应用测，不牵扯业务，坏了一眼能看出是哪层问题。
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.core import config
from backend.core.middleware import ApiKeyGate, BodyLimit, RateLimit, RequestContext


def _app(**mw) -> Starlette:
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/ping", ok, methods=["GET", "POST"])])
    for cls, kwargs in mw.items():
        app.add_middleware({"ratelimit": RateLimit, "bodylimit": BodyLimit,
                            "ctx": RequestContext}[cls], **kwargs)
    return app


def test_rate_limit_blocks_after_capacity():
    app = _app(ratelimit={"rpm": 3})
    with TestClient(app) as c:
        codes = [c.get("/ping").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429 and codes[4] == 429


def test_rate_limit_returns_retry_after_and_error_body():
    app = _app(ratelimit={"rpm": 1})
    with TestClient(app) as c:
        c.get("/ping")
        r = c.get("/ping")
    assert r.status_code == 429
    assert r.headers["retry-after"].isdigit()
    assert r.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_disabled_when_zero():
    app = _app(ratelimit={"rpm": 0})
    with TestClient(app) as c:
        assert all(c.get("/ping").status_code == 200 for _ in range(20))


def test_body_limit_rejects_large_payload(monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_BYTES", 128)
    app = _app(bodylimit={})
    with TestClient(app) as c:
        big = c.post("/ping", content=b"x" * 512,
                     headers={"content-type": "application/json",
                              "content-length": "512"})
        small = c.post("/ping", content=b"x",
                       headers={"content-type": "application/json",
                                "content-length": "1"})
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "BODY_TOO_LARGE"
    assert small.status_code == 200


def test_context_sets_request_id_and_timing():
    app = _app(ctx={})
    with TestClient(app) as c:
        r = c.get("/ping", headers={"x-request-id": "rid-1"})
    assert r.headers["x-request-id"] == "rid-1"
    assert float(r.headers["x-response-time-ms"]) >= 0


def test_context_generates_id_when_absent():
    app = _app(ctx={})
    with TestClient(app) as c:
        r = c.get("/ping")
    assert len(r.headers["x-request-id"]) >= 8


def test_api_key_gate_off_when_no_keys():
    app = Starlette(routes=[Route("/api/ping", lambda r: JSONResponse({"ok": True}))])
    app.add_middleware(ApiKeyGate, keys=[])
    with TestClient(app) as c:
        assert c.get("/api/ping").status_code == 200


def test_api_key_gate_rejects_missing_and_accepts_valid():
    app = Starlette(routes=[Route("/api/ping", lambda r: JSONResponse({"ok": True}))])
    app.add_middleware(ApiKeyGate, keys=["k-1"])
    with TestClient(app) as c:
        assert c.get("/api/ping").status_code == 401
        bad = c.get("/api/ping", headers={"x-api-key": "nope"})
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "UNAUTHORIZED"
        assert c.get("/api/ping", headers={"x-api-key": "k-1"}).status_code == 200
