"""会话存储：默认落盘到 SQLite，重启不丢「待确认的那一笔」。

为什么要落盘：演示现场最怕的就是「用户刚点确认，服务重启了，钱没动、状态也没了」。
会话里存的不是日志，而是**一笔已经过权限决策、等用户点确认的事务**，必须和审计一样持久。

两种后端：
  sqlite（默认）—— 单文件、WAL、进程重启后能还原；多 worker 也能共享
  memory        —— 单测用，快

stash 里会混进三类对象（Plan / Outcome / Decision），所以序列化时打类型标记，
反序列化按标记还原，不能用裸 json.dumps。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from backend.core import config

# ---------------------------------------------------------------- 编解码


def _json_default(o: Any) -> Any:
    """Decimal / 日期 / set 都要带类型标记——金额退回 float 就是精度事故。"""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(o, Decimal):
        return {"__dec": str(o)}
    if isinstance(o, (datetime, date)):
        return {"__date": o.isoformat()}
    if isinstance(o, (set, tuple)):
        return {"__list": list(o)}
    return str(o)


def _revive(o: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(o, list):
        return [_revive(x) for x in o]
    if isinstance(o, dict):
        if len(o) == 1:
            if "__dec" in o:
                return Decimal(str(o["__dec"]))
            if "__date" in o:
                raw = o["__date"]
                try:
                    return datetime.fromisoformat(raw)
                except ValueError:
                    return date.fromisoformat(raw)
            if "__list" in o:
                return [_revive(x) for x in o["__list"]]
        return {k: _revive(v) for k, v in o.items()}
    return o


def encode_stash(stash: dict[str, Any]) -> str:
    from backend.planner.contracts import Plan
    from backend.planner.executor import Outcome
    from backend.security.policy import Decision

    out: dict[str, Any] = {}
    for k, v in stash.items():
        if v is None:
            out[k] = None
        elif isinstance(v, Plan):
            out[k] = {"__t": "plan", "d": v.model_dump(mode="json")}
        elif isinstance(v, Outcome):
            out[k] = {"__t": "outcome", "d": asdict(v)}
        elif isinstance(v, Decision):
            out[k] = {"__t": "decision", "d": v.to_dict()}
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False, default=_json_default)


def decode_stash(raw: str) -> dict[str, Any]:
    from backend.planner.contracts import Plan
    from backend.planner.executor import Outcome
    from backend.security.policy import Decision

    if not raw:
        return {}
    data = _revive(json.loads(raw))
    for k, v in list(data.items()):
        if isinstance(v, dict) and "__t" in v:
            t, d = v["__t"], v["d"]
            if t == "plan":
                data[k] = Plan.model_validate(d)
            elif t == "outcome":
                data[k] = Outcome(**d)
            elif t == "decision":
                data[k] = Decision(**d)
            else:
                data[k] = d
    return data


# ---------------------------------------------------------------- 接口


class Store(Protocol):
    def load(self, sid: str) -> dict[str, Any] | None: ...
    def save(self, sid: str, state: str, stash: dict, history: list, device_trusted: bool,
             owner_uid: int = 0, owner_device: str = "", ver: int = 0) -> None: ...
    def claim(self, sid: str, ver: int, uid: int, new_stash: dict) -> bool: ...
    def finish(self, sid: str, state: str) -> None: ...
    def delete(self, sid: str) -> None: ...
    def purge(self, ttl: int) -> int: ...
    def count(self) -> int: ...


class MemoryStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def load(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._d.get(sid)
            return dict(rec) if rec else None

    def save(self, sid: str, state: str, stash: dict, history: list, device_trusted: bool,
             owner_uid: int = 0, owner_device: str = "", ver: int = 0) -> None:
        with self._lock:
            self._d[sid] = {"state": state, "stash": encode_stash(stash),
                            "history": json.dumps(history, ensure_ascii=False),
                            "device_trusted": int(device_trusted), "updated": time.time(),
                            "owner_uid": owner_uid, "owner_device": owner_device, "ver": ver}

    def claim(self, sid: str, ver: int, uid: int, new_stash: dict) -> bool:
        with self._lock:
            rec = self._d.get(sid)
            if not rec or rec["state"] != "AWAIT_CONFIRM" or rec["ver"] != ver \
                    or rec["owner_uid"] != uid:
                return False
            rec.update(state="EXECUTING", stash=encode_stash(new_stash),
                       ver=ver + 1, updated=time.time())
            return True

    def finish(self, sid: str, state: str) -> None:
        with self._lock:
            rec = self._d.get(sid)
            if rec:
                rec.update(state=state, updated=time.time())

    def delete(self, sid: str) -> None:
        with self._lock:
            self._d.pop(sid, None)

    def purge(self, ttl: int) -> int:
        cut = time.time() - ttl
        with self._lock:
            dead = [k for k, v in self._d.items() if v["updated"] < cut]
            for k in dead:
                self._d.pop(k, None)
            return len(dead)

    def count(self) -> int:
        with self._lock:
            return len(self._d)


class SqliteStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or config.SESSION_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                   sid TEXT PRIMARY KEY,
                   state TEXT NOT NULL,
                   stash TEXT NOT NULL,
                   history TEXT NOT NULL DEFAULT '[]',
                   device_trusted INTEGER NOT NULL DEFAULT 1,
                   updated REAL NOT NULL)""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated)")
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """加列式迁移：owner/ver 是后加的，旧库不重建。

        owner_uid/owner_device 让"会话"不再是唯一凭证；ver 用来做跨进程 CAS，
        挡住"用户连点两次确认"导致同一笔被扣两次。
        """
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        for name, ddl in (
            ("owner_uid", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_device", "TEXT NOT NULL DEFAULT ''"),
            ("ver", "INTEGER NOT NULL DEFAULT 0"),
            ("confirm_id", "TEXT"),
        ):
            if name not in have:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")

    def load(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT state, stash, history, device_trusted, updated, owner_uid,"
                " owner_device, ver, confirm_id FROM sessions WHERE sid=?",
                (sid,)).fetchone()
        if not r:
            return None
        return {"state": r["state"], "stash": r["stash"], "history": r["history"],
                "device_trusted": bool(r["device_trusted"]), "updated": r["updated"],
                "owner_uid": r["owner_uid"], "owner_device": r["owner_device"],
                "ver": r["ver"], "confirm_id": r["confirm_id"]}

    def save(self, sid: str, state: str, stash: dict, history: list, device_trusted: bool,
             owner_uid: int = 0, owner_device: str = "", ver: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions(sid, state, stash, history, device_trusted, updated,
                                        owner_uid, owner_device, ver)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sid) DO UPDATE SET
                     state=excluded.state, stash=excluded.stash, history=excluded.history,
                     device_trusted=excluded.device_trusted, updated=excluded.updated,
                     owner_uid=excluded.owner_uid, owner_device=excluded.owner_device,
                     ver=excluded.ver""",
                (sid, state, encode_stash(stash), json.dumps(history, ensure_ascii=False),
                 int(device_trusted), time.time(), owner_uid, owner_device, ver))
            self._conn.commit()

    def claim(self, sid: str, ver: int, uid: int, new_stash: dict) -> bool:
        """跨进程 CAS：把 AWAIT_CONFIRM 原子地翻成 EXECUTING。

        `threading.Lock` 跨进程无效，`SELECT ... FOR UPDATE` SQLite 也没有，
        所以用 `UPDATE ... WHERE ver=? AND state=?` 的 rowcount 做抢占。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET state='EXECUTING', ver=ver+1, stash=?, updated=?"
                " WHERE sid=? AND ver=? AND state='AWAIT_CONFIRM' AND owner_uid=?",
                (encode_stash(new_stash), time.time(), sid, ver, uid))
            self._conn.commit()
            return cur.rowcount == 1

    def finish(self, sid: str, state: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET state=?, updated=? WHERE sid=?",
                               (state, time.time(), sid))
            self._conn.commit()

    def delete(self, sid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            self._conn.commit()

    def purge(self, ttl: int) -> int:
        cut = time.time() - ttl
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE updated < ?", (cut,))
            self._conn.commit()
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def build() -> Store:
    if config.SESSION_BACKEND == "memory":
        return MemoryStore()
    return SqliteStore()
