"""服务端幂等：让"网络重试"和"用户连点"都只产生一笔业务。

原来的幂等键是 `f"{trace_id}-{step_id}"`，而 trace_id 是每个请求新生成的
`uuid4().hex[:16]`。客户端超时重试 → 新 trace → 新 key → 下游 `transfers.idem_key`
判重落空 → **重复转账**。幂等键必须由"这次业务请求的身份"决定，而不是由
"这次网络会话"决定。

两层防护：
- 请求层（本模块）：同一 (uid, key) 的 /chat 请求只执行一次，重放直接回放首次响应；
- 步骤层（gateway）：同一次业务内的多个下游写调用，用确定性 seed 派生 key，
  挡"请求已发出、响应丢了"这种半成功。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Literal

from backend.core import config

State = Literal["owner", "replay", "inflight", "conflict"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency(
    uid          INTEGER NOT NULL,
    scope        TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    request_hash TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    http_status  INTEGER,
    response     TEXT,
    created_at   REAL    NOT NULL,
    expires_at   REAL    NOT NULL,
    PRIMARY KEY(uid, scope, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_idem_exp ON idempotency(expires_at);
"""

_local = threading.local()


@dataclass
class Slot:
    state: State
    response: str | None = None
    http_status: int | None = None


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    c = sqlite3.connect(str(config.DATA_DIR / "app.db"), timeout=5.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    _local.c = c
    return c


def derive_key(uid: int, device_id: str, body: bytes) -> str:
    """在客户端未提供 X-Idempotency-Key 时派生兜底键。

    相同主体和相同请求体会被视为同一笔请求，因此短时间内连续提交相同转账时
    可能被合并。生产调用方应显式提供幂等键，接口说明见 docs/03-API契约.md。
    """
    h = hashlib.sha256()
    h.update(str(uid).encode())
    h.update(b"\x1f")
    h.update(device_id.encode())
    h.update(b"\x1f")
    h.update(body)
    return "auto:" + h.hexdigest()


def begin(uid: int, scope: str, key: str, request_hash: str,
          ttl: int | None = None) -> Slot:
    """抢占执行权，仅 owner 状态可以继续执行。

    ttl 区分两类键：
    - 客户端显式提供的 X-Idempotency-Key 使用长窗口，默认 24 小时；
    - 服务端派生的兜底键使用短窗口，只覆盖网络重试，
      否则用户上午和下午各转 500 元时，第二笔会被误判为重放。
    """
    life = ttl if ttl is not None else config.IDEMPOTENCY_TTL
    now = time.time()
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO idempotency(uid,scope,key,request_hash,status,"
            " created_at,expires_at) VALUES(?,?,?,?,'inflight',?,?)",
            (uid, scope, key, request_hash, now, now + life))
        if cur.rowcount == 1:
            # 本次就是第一个拿到这把锁的请求，直接归自己
            c.execute("COMMIT")
            return Slot("owner")

        row = c.execute("SELECT * FROM idempotency WHERE uid=? AND scope=? AND key=?",
                        (uid, scope, key)).fetchone()

        if row["request_hash"] != request_hash:
            c.execute("COMMIT")
            return Slot("conflict")

        if row["status"] == "done":
            c.execute("COMMIT")
            return Slot("replay", row["response"], row["http_status"])

        if row["status"] == "inflight" and \
                now - row["created_at"] < config.IDEMPOTENCY_INFLIGHT_TTL:
            c.execute("COMMIT")
            return Slot("inflight")

        # 上一次执行崩了（inflight 超时）或上次 failed：接管重试
        c.execute("UPDATE idempotency SET status='inflight', request_hash=?, created_at=?,"
                  " expires_at=? WHERE uid=? AND scope=? AND key=?",
                  (request_hash, now, now + life, uid, scope, key))
        c.execute("COMMIT")
        return Slot("owner")
    except sqlite3.Error:
        c.execute("ROLLBACK")
        raise


def finish(uid: int, scope: str, key: str, http_status: int, body: bytes) -> None:
    _conn().execute(
        "UPDATE idempotency SET status='done', http_status=?, response=? WHERE uid=? AND"
        " scope=? AND key=?",
        (http_status, body.decode("utf-8", "replace"), uid, scope, key))


def fail(uid: int, scope: str, key: str) -> None:
    """执行失败：标记 failed，允许客户端用同一个 key 重试。"""
    _conn().execute(
        "UPDATE idempotency SET status='failed' WHERE uid=? AND scope=? AND key=?",
        (uid, scope, key))


def purge_expired() -> int:
    cur = _conn().execute("DELETE FROM idempotency WHERE expires_at < ?", (time.time(),))
    return cur.rowcount


def replay_payload(slot: Slot) -> dict:
    try:
        return json.loads(slot.response or "{}")
    except ValueError:
        return {"ok": False, "error": {"code": "REPLAY_BROKEN", "message": "幂等回放失败"}}
