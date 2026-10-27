"""HTTP 层加固：体积限制的 chunked 绕过、版本别名、OpenAPI 去重、跨境判定。"""
from __future__ import annotations


def test_chunked_body_over_limit_is_rejected(client):
    """只信 Content-Length 是能被绕过的：分块传输不带这个头。"""
    r = client.post("/api/chat", content=b"x" * 70000,
                    headers={"Content-Type": "application/json",
                             "Transfer-Encoding": "chunked"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "BODY_TOO_LARGE"


def test_chunked_body_within_limit_still_works(client):
    """限流/限体积不能把正常的分块请求一起打死。"""
    body = '{"text":"有哪些自动续费"}'.encode()
    r = client.post("/api/chat", content=body,
                    headers={"Content-Type": "application/json",
                             "Transfer-Encoding": "chunked"})
    assert r.status_code == 200
    assert r.json()["card"]["type"] == "subs"


def test_version_alias_behaves_like_canonical(client):
    """/api/v1/* 是兼容别名，行为必须与 /api/* 完全一致。"""
    a = client.get("/api/health").json()
    b = client.get("/api/v1/health").json()
    assert a["ok"] is True and b["ok"] is True
    assert a["version"] == b["version"]

    r = client.post("/api/v1/chat", json={"text": "有哪些自动续费"})
    assert r.status_code == 200
    assert r.json()["card"]["type"] == "subs"


def test_openapi_does_not_duplicate_versioned_paths(client):
    """文档里每个接口只出现一次——原先是 /api 与 /api/v1 各一份，还刷 Duplicate ID 警告。"""
    spec = client.get("/openapi.json").json()
    paths = list(spec["paths"])
    assert not [p for p in paths if p.startswith("/api/v1")], paths
    assert "/api/chat" in paths
    assert max(p.count("/") for p in paths) >= 3      # 正常有多段路径，别被过滤没了


def test_cross_border_comes_from_data_not_bank_name(client, clean_bank):
    """跨境判定看核心系统给的标记，而不是"银行名里有没有 HSBC"。"""
    r = client.post("/api/chat", json={"text": "给赵敏转100"}).json()
    dec = (r.get("debug") or {}).get("decision") or {}
    assert dec.get("tier") == "HANDOFF", r
    assert dec.get("hard_rule") == "cross_border"

    # 同一句话换成境内收款人就不该转人工
    r2 = client.post("/api/chat", json={"text": "给王强转100"}).json()
    dec2 = (r2.get("debug") or {}).get("decision") or {}
    assert dec2.get("tier") != "HANDOFF", r2
