"""Mock 核心系统的库表与连接工具。

金额一律用 TEXT 存储（模拟真实银行以字符串传金额，规避浮点误差），
数据库内部计算用 Decimal，出口再格式化为两位小数字符串。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from backend.core.config import BANK_DB

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    segment TEXT NOT NULL DEFAULT '普通',      -- 普通 / 老年 / 高净值
    risk_level TEXT NOT NULL DEFAULT 'C3',     -- C1..C5
    phone TEXT,
    birthday TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                        -- 活期 / 定期
    last4 TEXT NOT NULL,
    balance TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY'
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    last4 TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '银联',
    daily_limit TEXT NOT NULL DEFAULT '50000.00',
    status TEXT NOT NULL DEFAULT 'active',     -- active / frozen / lost
    frozen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payees (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    phone TEXT,
    bank TEXT,
    account_last4 TEXT,
    relation TEXT,                             -- 母亲/父亲/同事/房东/朋友
    city TEXT DEFAULT '绵阳',
    blacklisted INTEGER NOT NULL DEFAULT 0,
    -- 是否跨境账户：由核心系统明确给出，不靠"银行名里有没有 HSBC"这种猜测
    cross_border INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,                          -- YYYY-MM-DD HH:MM:SS
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    sub_category TEXT,
    amount TEXT NOT NULL,
    direction TEXT NOT NULL,                   -- out / in
    city TEXT DEFAULT '绵阳',
    note TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    risk TEXT NOT NULL,                        -- R1..R5
    term_days INTEGER NOT NULL,                -- 0 表示活期/随时赎回
    min_amount TEXT NOT NULL,
    annual_rate TEXT NOT NULL,
    category TEXT NOT NULL,
    redeem_rule TEXT,
    tag TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'holding'
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    merchant TEXT NOT NULL,
    amount TEXT NOT NULL,
    cycle TEXT NOT NULL,                       -- 月/季/年
    first_charge TEXT,
    last_charge TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    payee_id INTEGER,
    amount TEXT NOT NULL,
    memo TEXT,
    kind TEXT NOT NULL,                        -- instant / scheduled / collect
    status TEXT NOT NULL,                      -- done / pending / rejected
    created_at TEXT NOT NULL,
    first_run_at TEXT,
    recurrence TEXT,
    idem_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'held',       -- held / released / settled
    expire_at TEXT,
    created_at TEXT NOT NULL,
    idem_key TEXT                              -- 幂等键：重复提交返回首次结果
);

CREATE TABLE IF NOT EXISTS gift_orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    gift_id TEXT NOT NULL,
    name TEXT NOT NULL,
    amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or BANK_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 读写混合 + 高频转账：WAL 让读不阻塞写，NORMAL 去掉每次提交的 fsync
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """加列式迁移：已有 bank.db 不必 --reset 也能用上新列。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(holds)")}
    if "idem_key" not in have:
        conn.execute("ALTER TABLE holds ADD COLUMN idem_key TEXT")
    have_p = {r["name"] for r in conn.execute("PRAGMA table_info(payees)")}
    if "cross_border" not in have_p:
        conn.execute("ALTER TABLE payees ADD COLUMN cross_border INTEGER NOT NULL DEFAULT 0")
        # 旧库补齐：跨境账户只认城市，不猜银行名
        conn.execute("UPDATE payees SET cross_border=1 WHERE city IN ('香港','澳门','台湾')")
    # 幂等键唯一（部分索引：NULL 不参与唯一性，历史行不受影响）
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_holds_idem ON holds(idem_key)"
                 " WHERE idem_key IS NOT NULL")


# ---------- 金额工具 ----------

def D(x) -> Decimal:
    return Decimal(str(x))


def mq(x) -> str:
    """quantize 到分，输出两位小数字符串。"""
    return str(D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
