"""会话状态机 + 待确认暂存。

状态：IDLE → (CLARIFY | CHOOSE | AWAIT_CONFIRM) → IDLE
关键约束：AWAIT_CONFIRM 状态下只接受"确认 / 取消 / 改金额"三类输入，
其他指令一律先取消待确认事务再处理——防止确认态被注入新指令劫持。

持久化由 `store` 负责：默认落盘，进程重启后「等确认的那一笔」仍然在。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.core import config
from backend.session import store as store_mod

TTL = config.SESSION_TTL
CONFIRM_TTL = config.CONFIRM_TTL
TOUCH_INTERVAL = 10.0      # 同一会话 10 秒内不重复写库，避免每轮对话都落一次盘

STORE: store_mod.Store = store_mod.build()


def configure(s: store_mod.Store) -> None:
    """测试或多进程场景下替换存储实现。"""
    global STORE
    STORE = s


@dataclass
class Session:
    sid: str
    state: str = "IDLE"
    stash: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    updated: float = field(default_factory=time.time)
    device_trusted: bool = True

    def expire_pending(self) -> bool:
        ts = self.stash.get("created_at")
        if ts and time.time() - ts > CONFIRM_TTL:
            self.state = "IDLE"
            self.stash = {}
            save(self)
            return True
        return False


def _persist(s: Session) -> None:
    # 只读往返（账单/查询）不产生任何待还原状态，没必要每轮都落盘
    if s.state == "IDLE" and not s.stash:
        return
    STORE.save(s.sid, s.state, s.stash, s.history, s.device_trusted)


def get(sid: str | None) -> Session:
    """取会话：内存命中就直接用；否则从存储还原；都没有就新建。"""
    now = time.time()
    if not sid:
        sid = uuid.uuid4().hex[:12]
        s = Session(sid=sid)
        _persist(s)
        return s

    rec = STORE.load(sid)
    if rec is None:
        s = Session(sid=sid)
        _persist(s)
        return s

    if now - float(rec.get("updated") or 0) > TTL:
        STORE.delete(sid)
        s = Session(sid=sid)
        _persist(s)
        return s

    s = Session(sid=sid, state=rec.get("state") or "IDLE",
                stash=store_mod.decode_stash(rec.get("stash") or ""),
                history=_load_json(rec.get("history")),
                updated=float(rec.get("updated") or now),
                device_trusted=bool(rec.get("device_trusted", True)))
    return s


def _load_json(raw: Any) -> list:
    import json
    try:
        return json.loads(raw) if isinstance(raw, str) and raw else list(raw or [])
    except (ValueError, TypeError):
        return []


def save(s: Session) -> None:
    s.updated = time.time()
    _persist(s)


def reset(s: Session) -> None:
    s.state = "IDLE"
    s.stash = {}
    s.history = []
    # 待确认事务被撤销时必须真的从库里删掉，否则重启后还会冒出来
    STORE.delete(s.sid)


def ask(s: Session, kind: str, payload: dict) -> None:
    """进入澄清 / 选择状态，记住上下文等下一句。"""
    s.state = kind          # CLARIFY / CHOOSE
    s.stash = {**payload, "created_at": time.time()}
    save(s)


def await_confirm(s: Session, payload: dict) -> None:
    s.state = "AWAIT_CONFIRM"
    s.stash = {**payload, "created_at": time.time()}
    save(s)


def touch(s: Session) -> None:
    """刷新活跃时间（限频），让 TTL 只淘汰真正闲置的会话。"""
    if time.time() - s.updated >= TOUCH_INTERVAL:
        _persist(s)


def purge_expired() -> int:
    return STORE.purge(TTL)
