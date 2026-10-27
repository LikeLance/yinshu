"""审计日志：哈希链 + 可校验。

每行 self_hash = sha256(prev_hash | action | payload_hash | result)，
任何一行被篡改，后续链条全部对不上，verify() 会指出断点。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from threading import Lock

from backend.core.config import APP_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    self_hash TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace ON audit_logs(trace_id);
"""

_lock = Lock()
_conn = sqlite3.connect(str(APP_DB), check_same_thread=False)
_conn.row_factory = sqlite3.Row
# WAL + NORMAL：一次请求要写 2~3 条审计，默认日志模式每条都 fsync（Windows 上 20~30ms），
# 开 WAL 后台检查点后单请求耗时降一个量级；崩溃最多丢最后几条，哈希链本身不会断。
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.executescript(SCHEMA)
_conn.commit()
GENESIS = "0" * 64


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def append(trace_id: str, action: str, payload: dict, result: str = "ok", actor: str = "agent") -> dict:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    ph = _h(body)
    with _lock:
        last = _conn.execute("SELECT self_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["self_hash"] if last else GENESIS
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self_hash = _h(f"{prev}|{action}|{ph}|{result}")
        _conn.execute(
            "INSERT INTO audit_logs(trace_id,ts,actor,action,payload,payload_hash,prev_hash,self_hash,result)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trace_id, ts, actor, action, body, ph, prev, self_hash, result))
        _conn.commit()
        rid = _conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    return {"id": rid, "seq_hash": self_hash[:12], "ts": ts}


def history(trace_id: str) -> list[dict]:
    rows = _conn.execute("SELECT * FROM audit_logs WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["prev_hash"] = d["prev_hash"][:12]
        d["self_hash"] = d["self_hash"][:12]
        d["payload_hash"] = d["payload_hash"][:12]
        out.append(d)
    return out


def verify() -> dict:
    rows = _conn.execute("SELECT * FROM audit_logs ORDER BY id").fetchall()
    prev = GENESIS
    for r in rows:
        expect = _h(f"{prev}|{r['action']}|{r['payload_hash']}|{r['result']}")
        if r["prev_hash"] != prev or r["self_hash"] != expect:
            return {"ok": False, "total": len(rows), "broken_at": r["id"], "trace_id": r["trace_id"]}
        prev = r["self_hash"]
    return {"ok": True, "total": len(rows), "tail": prev[:12]}


def stats() -> dict:
    total = _conn.execute("SELECT COUNT(*) c FROM audit_logs").fetchone()["c"]
    denied = _conn.execute("SELECT COUNT(*) c FROM audit_logs WHERE result='deny'").fetchone()["c"]
    handoff = _conn.execute("SELECT COUNT(*) c FROM audit_logs WHERE result='handoff'").fetchone()["c"]
    blocked = _conn.execute("SELECT COUNT(*) c FROM audit_logs WHERE result='blocked'").fetchone()["c"]
    return {"total": total, "deny": denied, "handoff": handoff, "injection_blocked": blocked}
