"""身份层：谁是操作主体。

原实现的三个问题，都在这里收口：

1. **身份来自请求体**。`ChatReq` 里没有 user_id，用户身份写死在 `DEMO_USER_ID`；
   而 `device_trusted` 由客户端自报——攻击者传 `true` 就把"非常用设备 +15 分"
   这个风控特征直接废掉。现在设备信任由服务端按 (uid, device_id) 的历史推导。
2. **会话即凭证**。`session_id` 是唯一的"认证物"，拿到一个处于 AWAIT_CONFIRM 的 sid
   发"确认"就能触发那笔转账。现在会话绑定 owner，跨主体直接 409。
3. **审计没有主体**。actor 恒为 "agent"，出事查不到是谁。现在 Principal 进审计哈希。

只用标准库手写 HS256 JWT：项目约束是零新增依赖，而 JWT 的签名部分就是
`base64url + hmac-sha256`，自己写比引入 pyjwt 更可控（也更容易被审阅）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Mapping

from backend.core import config
from backend.core.errors import YinshuError


class AuthError(YinshuError):
    code = "UNAUTHORIZED"
    user_msg = "身份校验未通过，请重新登录。"


class IdentityInBody(AuthError):
    code = "IDENTITY_IN_BODY"
    user_msg = "身份信息不能放在请求体里。"


@dataclass(frozen=True)
class Principal:
    """一次请求的操作主体。uid=0 表示系统自身（后台任务、锚定）。"""

    uid: int
    device_id: str
    channel: str = "app"
    ip: str = ""
    jti: str = ""

    @property
    def actor_name(self) -> str:
        return f"user:{self.uid}" if self.uid else "system"

    @classmethod
    def system(cls) -> "Principal":
        return cls(0, "", "system", "", "")

    @classmethod
    def demo(cls, ip: str = "") -> "Principal":
        return cls(config.DEMO_USER_ID, config.DEMO_DEVICE, "demo", ip, "")


# ---------------------------------------------------------------- JWT (HS256)

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(msg: str) -> str:
    return _b64(hmac.new(config.JWT_SECRET, msg.encode("ascii"), hashlib.sha256).digest())


def issue_token(uid: int, device_id: str, channel: str = "app",
                ttl: int | None = None) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({
        "sub": str(uid), "did": device_id, "ch": channel,
        "iat": now, "exp": now + (ttl if ttl is not None else config.JWT_TTL),
        "jti": secrets.token_hex(8),
    }, separators=(",", ":")).encode())
    return f"{header}.{payload}.{_sign(f'{header}.{payload}')}"


def verify_token(token: str, ip: str = "") -> Principal:
    try:
        header, payload, sig = token.split(".")
    except ValueError as e:
        raise AuthError("token 结构不合法") from e

    if not hmac.compare_digest(_sign(f"{header}.{payload}"), sig):
        raise AuthError("签名不匹配")

    try:
        claims = json.loads(_unb64(payload))
        uid = int(claims["sub"])
        exp = int(claims["exp"])
    except (ValueError, KeyError, TypeError) as e:
        raise AuthError("token 载荷不合法") from e

    if exp < time.time():
        raise AuthError("token 已过期")
    if int(claims.get("iat", 0)) > time.time() + 60:
        raise AuthError("token 生效时间在未来")

    return Principal(uid, str(claims.get("did") or ""), str(claims.get("ch") or "app"),
                     ip, str(claims.get("jti") or ""))


# ---------------------------------------------------------------- 设备信任

_local = threading.local()


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    c = sqlite3.connect(str(config.DATA_DIR / "app.db"), timeout=5.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("""CREATE TABLE IF NOT EXISTS devices(
        uid INTEGER NOT NULL, device_id TEXT NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        ok_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(uid, device_id))""")
    _local.c = c
    return c


def device_trusted(uid: int, device_id: str) -> bool:
    """设备是否可信：由服务端历史判定，不接受客户端自述。

    规则：该设备此前成功完成过资金类操作（ok_count>=1）才算可信。
    首次使用的新设备一律按"不可信"处理（风控 +15 分，可能触发确认）。
    """
    if config.AUTH_MODE == "off":
        return True                      # 演示/测试模式不做设备画像
    if not device_id:
        return False
    row = _conn().execute(
        "SELECT ok_count FROM devices WHERE uid=? AND device_id=?", (uid, device_id)).fetchone()
    return bool(row and row["ok_count"] >= 1)


def touch_device(uid: int, device_id: str, success: bool = False) -> None:
    if config.AUTH_MODE == "off" or not device_id:
        return
    now = time.time()
    c = _conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(
            "INSERT INTO devices(uid,device_id,first_seen,last_seen,ok_count) VALUES(?,?,?,?,?)"
            " ON CONFLICT(uid,device_id) DO UPDATE SET last_seen=excluded.last_seen,"
            " ok_count=ok_count+excluded.ok_count",
            (uid, device_id, now, now, 1 if success else 0))
        c.execute("COMMIT")
    except sqlite3.Error:
        c.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 入口

def principal_from_headers(headers: Mapping[str, str], ip: str = "") -> Principal:
    """从 Authorization 头解析主体。身份只认 header，不认 body。

    演示模式（AUTH_MODE=off）下 uid 固定取 DEMO_USER_ID，但仍记录调用方自带的
    X-Device-Id——审计里要能看到"哪台设备来的"，只是不做可信判定。
    """
    mode = config.AUTH_MODE
    auth = (headers.get("authorization") or "").strip()
    device = (headers.get("x-device-id") or "").strip() or config.DEMO_DEVICE

    if mode == "off":
        return Principal(config.DEMO_USER_ID, device, "demo", ip, "")

    if not auth.startswith("Bearer "):
        if mode == "required":
            raise AuthError("缺少 Authorization: Bearer <token>")
        return Principal(config.DEMO_USER_ID, device, "demo", ip, "")

    return verify_token(auth[7:].strip(), ip)
