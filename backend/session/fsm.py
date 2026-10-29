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
from backend.core.errors import YinshuError
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
    # 会话归属：sid 不再是唯一凭证，跨主体访问直接拒绝
    owner_uid: int = 0
    owner_device: str = ""
    ver: int = 0                     # 乐观锁版本号，用于确认态抢占

    def expire_pending(self) -> bool:
        ts = self.stash.get("created_at")
        if ts and time.time() - ts > CONFIRM_TTL:
            self.state = "IDLE"
            self.stash = {}
            # 必须真删库：`_persist` 对「IDLE + 空 stash」是跳过的，只清内存的话
            # 库里那条 AWAIT_CONFIRM 会一直活着 —— 之后每次带同一个 sid 进来都会
            # 又读到它、又判超时，用户卡在"已超时"循环里直到会话 TTL 才自愈。
            STORE.delete(self.sid)
            return True
        return False


class OwnerMismatch(YinshuError):
    code = "SESSION_OWNER_MISMATCH"
    user_msg = "这个会话不属于当前用户，请重新发起。"


def _persist(s: Session) -> None:
    # 只读往返（账单/查询）不产生任何待还原状态，没必要每轮都落盘
    if s.state == "IDLE" and not s.stash:
        return
    STORE.save(s.sid, s.state, s.stash, s.history, s.device_trusted,
               owner_uid=s.owner_uid, owner_device=s.owner_device, ver=s.ver)


def _bind(s: Session, principal: Any) -> Session:
    """把会话绑到主体。旧库（owner_uid=0）首次访问时接管，之后不可换人。"""
    uid = int(getattr(principal, "uid", 0) or 0)
    device = str(getattr(principal, "device_id", "") or "")
    if s.owner_uid == 0:
        s.owner_uid, s.owner_device = uid, device
    elif s.owner_uid != uid:
        raise OwnerMismatch(f"sid={s.sid} owner={s.owner_uid} caller={uid}")
    return s


def get(sid: str | None, principal: Any = None) -> Session:
    """取会话：存储命中则还原；否则新建。跨主体访问抛 OwnerMismatch。"""
    from backend.security.identity import Principal

    p = principal or Principal.demo()
    now = time.time()
    if not sid:
        sid = uuid.uuid4().hex[:12]
        s = _bind(Session(sid=sid), p)
        _persist(s)
        return s

    rec = STORE.load(sid)
    if rec is None:
        s = _bind(Session(sid=sid), p)
        _persist(s)
        return s

    if now - float(rec.get("updated") or 0) > TTL:
        STORE.delete(sid)
        s = _bind(Session(sid=sid), p)
        _persist(s)
        return s

    s = Session(sid=sid, state=rec.get("state") or "IDLE",
                stash=store_mod.decode_stash(rec.get("stash") or ""),
                history=_load_json(rec.get("history")),
                updated=float(rec.get("updated") or now),
                device_trusted=bool(rec.get("device_trusted", True)),
                owner_uid=int(rec.get("owner_uid") or 0),
                owner_device=rec.get("owner_device") or "",
                ver=int(rec.get("ver") or 0))
    return _bind(s, p)


def claim(s: Session, principal: Any, new_stash: dict) -> bool:
    """确认态抢占（跨进程 CAS）：只有获得执行权的一方可以执行该事务。

    用户连点两次确认 / 两个 worker 同时收到确认时，第二次返回 False。
    """
    uid = int(getattr(principal, "uid", 0) or 0)
    ok = STORE.claim(s.sid, s.ver, uid, new_stash)
    if ok:
        s.state = "EXECUTING"
        s.ver += 1
        s.stash = new_stash
    return ok


def finish(s: Session, state: str = "EXECUTED") -> None:
    s.state = state
    STORE.finish(s.sid, state)


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
    """刷新活跃时间（限频），使 TTL 只淘汰闲置会话。"""
    if time.time() - s.updated >= TOUCH_INTERVAL:
        _persist(s)


def purge_expired() -> int:
    return STORE.purge(TTL)
