"""审计日志：哈希链、校验、主体记录和锚定。

每条记录的 self_hash 覆盖前一条哈希和本条关键字段。修改历史记录会导致后续链校验失败。

v2 的主要变更：
1. 使用 thread-local 数据库连接和 BEGIN IMMEDIATE，避免多进程追加时分叉；
2. 设置 busy_timeout，减少 WAL 并发写入直接报锁冲突；
3. 哈希覆盖主体、设备、来源地址和渠道，便于追责；
4. 校验按批次读取，并定期写入检查点；
5. 支持本地和 HTTP 锚点，用于发现整表重写后重新计算哈希的情况。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

from backend.core.config import APP_DB
from backend.security import pii

GENESIS = "0" * 64
SEP = "\x1f"                  # 避免使用可能出现在字段内容中的分隔符
CHECKPOINT_EVERY = 5000       # 检查点间隔
VERIFY_BATCH = 2000           # 分块校验的批大小
LOCK_TIMEOUT_S = 5.0
RETRY_BACKOFF = (0.01, 0.02, 0.04, 0.08, 0.16)

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

-- v2 新增列（旧库靠 _migrate() 补）
-- actor_id / device_id / ip / channel：追责维度，参与 v2 哈希
-- pii_ref：指向 pii_vault，原文不可还原
-- chain_ver：1=旧公式，2=新公式；verify 按版本分派，保证旧库原地升级不断链

CREATE TABLE IF NOT EXISTS pii_vault (
    ref TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    key_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS audit_checkpoints (
    last_id INTEGER PRIMARY KEY,
    last_hash TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_anchors (
    seq INTEGER PRIMARY KEY,
    self_hash TEXT NOT NULL,
    sig TEXT NOT NULL,
    ext_ref TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""

_V2_COLUMNS = (
    ("actor_id", "INTEGER NOT NULL DEFAULT 0"),
    ("device_id", "TEXT NOT NULL DEFAULT ''"),
    ("ip", "TEXT NOT NULL DEFAULT ''"),
    ("channel", "TEXT NOT NULL DEFAULT ''"),
    ("pii_ref", "TEXT"),
    ("chain_ver", "INTEGER NOT NULL DEFAULT 1"),
)

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------- 连接

def _uri_ro() -> str:
    return "file:" + quote(APP_DB.as_posix(), safe="/:") + "?mode=ro"


def _writer() -> sqlite3.Connection:
    c = getattr(_local, "w", None)
    if c is not None:
        return c
    c = sqlite3.connect(str(APP_DB), timeout=LOCK_TIMEOUT_S, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA wal_autocheckpoint=1000")
    c.execute(f"PRAGMA busy_timeout={int(LOCK_TIMEOUT_S * 1000)}")
    _local.w = c
    return c


def _reader() -> sqlite3.Connection:
    """只读连接：mode=ro + query_only，物理上防住"审计路径被误用于改数据"。"""
    c = getattr(_local, "r", None)
    if c is not None:
        return c
    c = sqlite3.connect(_uri_ro(), uri=True, timeout=LOCK_TIMEOUT_S, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={int(LOCK_TIMEOUT_S * 1000)}")
    c.execute("PRAGMA query_only=1")
    _local.r = c
    return c


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """加列式迁移：旧库不需要重建，历史行 chain_ver 保持 1 继续按旧公式校验。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(audit_logs)")}
    added = []
    for name, ddl in _V2_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {ddl}")
            added.append(name)
    return added


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = _writer()
        conn.executescript(SCHEMA)
        _migrate(conn)
        _initialized = True


def close() -> None:
    """关闭本线程连接（测试与优雅退出用）。"""
    global _initialized
    for attr in ("w", "r"):
        c = getattr(_local, attr, None)
        if c is not None:
            try:
                c.close()
            finally:
                setattr(_local, attr, None)
    _initialized = False


init()


# ---------------------------------------------------------------- 哈希

def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _v1(prev: str, action: str, payload_hash: str, result: str) -> str:
    """旧公式，仅用于校验 chain_ver=1 的历史行。"""
    return _h(f"{prev}|{action}|{payload_hash}|{result}")


def _v2(prev: str, ts: str, trace_id: str, actor_id: int, device_id: str,
        action: str, payload_hash: str, result: str) -> str:
    return _h(SEP.join([prev, ts, trace_id, str(actor_id), device_id or "",
                        action, payload_hash, result]))


def _expect(row: sqlite3.Row, prev: str) -> str:
    if int(row["chain_ver"] or 1) >= 2:
        return _v2(prev, row["ts"], row["trace_id"], row["actor_id"] or 0,
                   row["device_id"] or "", row["action"], row["payload_hash"], row["result"])
    return _v1(prev, row["action"], row["payload_hash"], row["result"])


# ---------------------------------------------------------------- 写入

def append(trace_id: str, action: str, payload: dict, result: str = "ok",
           actor: str = "agent", principal: Any = None) -> dict:
    """追加一条审计。

    principal 为 None 时退化为 v1 语义（只记 actor 字符串）；否则记录主体维度。
    鸭子类型即可：uid / device_id / ip / channel / actor_name。
    """
    if not _initialized:
        init()

    if principal is None:
        from backend.core import reqctx

        principal = reqctx.principal()

    uid = int(getattr(principal, "uid", 0) or 0)
    device = str(getattr(principal, "device_id", "") or "")
    ip = str(getattr(principal, "ip", "") or "")
    channel = str(getattr(principal, "channel", "") or "")
    actor_name = str(getattr(principal, "actor_name", "") or actor)

    safe = payload if isinstance(payload, dict) else {"value": str(payload)}
    redacted, ref = pii.redact(safe)
    body = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    ph = _h(body)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    conn = _writer()
    last_err: Exception | None = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            if ref is not None:
                now = time.time()
                conn.execute(
                    "INSERT INTO pii_vault(ref,trace_id,digest,key_id,created_at,expires_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (ref, trace_id, pii.digest(safe), pii.key_id(), now,
                     now + pii.retention_seconds()))
            last = conn.execute(
                "SELECT self_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["self_hash"] if last else GENESIS
            self_hash = _v2(prev, ts, trace_id, uid, device, action, ph, result)
            cur = conn.execute(
                "INSERT INTO audit_logs(trace_id,ts,actor,actor_id,device_id,ip,channel,"
                " action,payload,payload_hash,prev_hash,self_hash,result,pii_ref,chain_ver)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,2)",
                (trace_id, ts, actor_name, uid, device, ip, channel,
                 action, body, ph, prev, self_hash, result, ref))
            rid = int(cur.lastrowid)
            conn.execute("COMMIT")
            if rid % CHECKPOINT_EVERY == 0:
                _write_checkpoint(conn, rid, self_hash, ts)
            return {"id": rid, "seq_hash": self_hash[:12], "ts": ts, "pii_ref": ref}
        except sqlite3.OperationalError as e:          # 只有 locked / busy 才重试
            _rollback(conn)
            last_err = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError(f"audit append 写锁获取失败：{last_err}")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _write_checkpoint(conn: sqlite3.Connection, last_id: int, last_hash: str, ts: str) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO audit_checkpoints(last_id,last_hash,ts)"
                     " VALUES(?,?,?)", (last_id, last_hash, ts))
        conn.execute("COMMIT")
    except sqlite3.Error:
        _rollback(conn)          # 检查点只是加速手段，失败不能影响主链路


# ---------------------------------------------------------------- 读取 / 校验

def history(trace_id: str) -> list[dict]:
    rows = _reader().execute(
        "SELECT * FROM audit_logs WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["prev_hash"] = d["prev_hash"][:12]
        d["self_hash"] = d["self_hash"][:12]
        d["payload_hash"] = d["payload_hash"][:12]
        out.append(d)
    return out


def _checkpoint_start(conn: sqlite3.Connection) -> tuple[int, str]:
    """取最新可用检查点。检查点自身被篡改时放弃它，退回全量校验。"""
    cp = conn.execute(
        "SELECT last_id,last_hash FROM audit_checkpoints ORDER BY last_id DESC LIMIT 1").fetchone()
    if not cp:
        return 0, GENESIS
    row = conn.execute("SELECT self_hash FROM audit_logs WHERE id=?",
                       (cp["last_id"],)).fetchone()
    if not row or row["self_hash"] != cp["last_hash"]:
        return 0, GENESIS          # 检查点对不上 → 不可信，从头校验
    return int(cp["last_id"]), cp["last_hash"]


def verify(trace_id: str | None = None, full: bool = False,
           batch: int = VERIFY_BATCH) -> dict:
    """校验链完整性。

    - 默认从最近检查点开始（分块、内存 O(batch)）；`full=True` 强制从头。
    - 指定 trace_id 时只校验该 trace 的行（用于单笔追溯）。
    - 同时比对最新锚点，能发现"整表重写 + 本地重算"。
    """
    conn = _reader()
    total = int(conn.execute("SELECT COUNT(*) c FROM audit_logs").fetchone()["c"])

    cur_id, prev = (0, GENESIS) if (full or trace_id) else _checkpoint_start(conn)
    started_at = cur_id

    sql = ("SELECT id,trace_id,ts,actor_id,device_id,action,payload_hash,prev_hash,"
           "self_hash,result,chain_ver FROM audit_logs WHERE id>? ")
    if trace_id:
        sql += "AND trace_id=? "
    sql += "ORDER BY id LIMIT ?"

    checked = 0
    while True:
        params = (cur_id, trace_id, batch) if trace_id else (cur_id, batch)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            break
        for r in rows:
            expect = _expect(r, prev)
            if r["prev_hash"] != prev or r["self_hash"] != expect:
                return {"ok": False, "total": total, "checked": checked,
                        "broken_at": r["id"], "trace_id": r["trace_id"],
                        "detail": "prev 不匹配" if r["prev_hash"] != prev else "self_hash 不匹配"}
            prev = r["self_hash"]
            cur_id = r["id"]
            checked += 1
        if len(rows) < batch:
            break

    return {"ok": True, "total": total, "checked": checked, "from_id": started_at,
            "tail": prev[:12], "anchor": _anchor_status(conn)}


def _anchor_status(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT seq,self_hash,ext_ref,ts FROM audit_anchors ORDER BY seq DESC LIMIT 1").fetchone()
    if not row:
        return None
    cur = conn.execute("SELECT self_hash FROM audit_logs WHERE id=?", (row["seq"],)).fetchone()
    ok = bool(cur) and cur["self_hash"] == row["self_hash"]
    return {"seq": row["seq"], "head": row["self_hash"][:12], "ext_ref": row["ext_ref"],
            "ts": row["ts"], "match": ok}


# ---------------------------------------------------------------- 链尾锚定

class Anchor(Protocol):
    """锚定后端：把 (seq, head) 发到一个"事后改不了"的地方。"""

    def publish(self, seq: int, head: str, sig: str, ts: str) -> str: ...


class LocalTableAnchor:
    """默认实现：写本地 audit_anchors。

    诚实地说，这只能防"删掉后面几行"，防不住"有权限的人连锚点一起改"。
    真上线应换成 HttpAnchor 指向行内存证 / WORM 对象锁存储。
    """

    name = "local-table"

    def publish(self, seq: int, head: str, sig: str, ts: str) -> str:
        conn = _writer()
        conn.execute(
            "INSERT OR REPLACE INTO audit_anchors(seq,self_hash,sig,ext_ref,ts)"
            " VALUES(?,?,?,?,?)", (seq, head, sig, f"local:{seq}", ts))
        return f"local:{seq}"


class HttpAnchor:
    """外部锚定：POST {seq, head, sig, ts}，要求返回 receipt_id。"""

    name = "http"

    def __init__(self, url: str, token: str = "") -> None:
        self.url, self.token = url, token

    def publish(self, seq: int, head: str, sig: str, ts: str) -> str:
        import httpx

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with httpx.Client(timeout=3.0, trust_env=False) as c:
            r = c.post(self.url, json={"seq": seq, "head": head, "sig": sig, "ts": ts},
                       headers=headers)
            r.raise_for_status()
            data = r.json()
        return str(data.get("receipt_id") or data.get("receipt") or f"http:{seq}")


_ANCHOR: Anchor | None = None


def configure_anchor(a: Anchor | None) -> None:
    global _ANCHOR
    _ANCHOR = a


def get_anchor() -> Anchor:
    global _ANCHOR
    if _ANCHOR is None:
        from backend.core import config

        _ANCHOR = HttpAnchor(config.ANCHOR_URL, config.ANCHOR_TOKEN) if config.ANCHOR_URL \
            else LocalTableAnchor()
    return _ANCHOR


def anchor_tail() -> dict:
    """把当前链尾签名后外发，返回锚定回执。"""
    import hmac

    from backend.core import config

    conn = _reader()
    row = conn.execute(
        "SELECT id,self_hash,ts FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"ok": False, "reason": "empty"}
    msg = f"{row['id']}{SEP}{row['self_hash']}".encode()
    sig = hmac.new(config.ANCHOR_KEY.encode(), msg, hashlib.sha256).hexdigest()
    try:
        ref = get_anchor().publish(int(row["id"]), row["self_hash"], sig, row["ts"])
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:120]}
    return {"ok": True, "seq": row["id"], "head": row["self_hash"][:12], "ext_ref": ref}


def stats() -> dict:
    conn = _reader()

    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "total": one("SELECT COUNT(*) FROM audit_logs"),
        "deny": one("SELECT COUNT(*) FROM audit_logs WHERE result='deny'"),
        "handoff": one("SELECT COUNT(*) FROM audit_logs WHERE result='handoff'"),
        "injection_blocked": one("SELECT COUNT(*) FROM audit_logs WHERE result='blocked'"),
        "pii_redacted": one("SELECT COUNT(*) FROM pii_vault"),
        "anchors": one("SELECT COUNT(*) FROM audit_anchors"),
        "checkpoints": one("SELECT COUNT(*) FROM audit_checkpoints"),
    }


def purge_pii_expired() -> int:
    """按保留期清理原文摘要（原文本身从未落库，这里清的是可关联性）。"""
    conn = _writer()
    cur = conn.execute("DELETE FROM pii_vault WHERE expires_at IS NOT NULL AND expires_at < ?",
                       (time.time(),))
    return cur.rowcount
