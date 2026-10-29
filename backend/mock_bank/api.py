"""银枢 Mock 核心系统 —— 接口形态刻意贴近真实银行。

设计要点（对应方案 5.2）：
- 写操作强制 Idempotency-Key，重复提交返回首次结果；
- 金额走字符串，避免浮点；
- 有限额校验、T+1 申购、待确认份额；
- 小额随机 SYSTEM_BUSY，用来验证上游重试与补偿。

启动：python -m uvicorn backend.mock_bank.api:app --port 8010
"""
from __future__ import annotations

import json
import random
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from contextvars import ContextVar

from fastapi import Body, FastAPI, Header, HTTPException, Query
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.middleware.cors import CORSMiddleware

from backend.mock_bank.db import D, connect, init_db, mq

app = FastAPI(title="银枢 Mock 核心系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_conn = connect()
init_db(_conn)

SINGLE_LIMIT = D("50000")
DAILY_LIMIT = D("200000")
NOISE_RATE = 0.0          # 演示默认 0，跑稳定性测试时可调高


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _uid(x_user_id: str | None) -> int:
    """调用方主体。

    这是**内网跳**：上游网关把自己校验过的 uid 放 `X-User-Id` 传下来，
    核心系统据此做数据隔离与归属校验。Mock 核心只监听 127.0.0.1，
    真实银行这一跳应走 mTLS / 服务网格身份，不能靠裸 header。
    """
    try:
        return int(x_user_id) if x_user_id else 1
    except ValueError:
        raise HTTPException(400, {"code": "BAD_USER", "message": "X-User-Id 非法"})


def _own(table: str, row_id: int, owner: int, what: str) -> None:
    """归属校验：不是自己的资源一律拒绝，**失败关闭**。

    没有这一步，一旦上游把账户/收款人传错，"多租户"就会变成"跨用户动钱"。
    宁可报错，也不能猜。
    """
    r = _conn.execute(f"SELECT user_id FROM {table} WHERE id=?", (row_id,)).fetchone()
    if not r:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": f"{what}不存在"})
    if int(r["user_id"]) != owner:
        raise HTTPException(403, {"code": "FORBIDDEN", "message": f"{what}不属于当前用户"})


_UID: ContextVar[int] = ContextVar("mock_uid", default=1)


def uid() -> int:
    """当前请求的操作主体，由 Identity 中间件从 X-User-Id 注入。"""
    return _UID.get()


class Identity(BaseHTTPMiddleware):
    """把上游网关校验过的主体注入请求上下文。

    Mock 核心只监听 127.0.0.1，这一跳的信任来自"内网 + 网关已鉴权"；
    真实银行应换成 mTLS / 服务网格身份，而不是裸 header。

    为什么不用 `?user_id=` 查询参数：那是**可伪造**的，
    传一个别人的 id 就能读别人的账户。身份只能来自请求上下文。
    """

    async def dispatch(self, request, call_next):
        token = _UID.set(_uid(request.headers.get("x-user-id")))
        try:
            return await call_next(request)
        finally:
            _UID.reset(token)


app.add_middleware(Identity)


def _default_account(owner: int | None = None) -> int:
    """调用方的默认扣款账户（存在活期就优先活期）。多账户时不做猜测，只按类型取。"""
    owner = uid() if owner is None else owner
    row = _conn.execute(
        "SELECT id FROM accounts WHERE user_id=? ORDER BY (kind='活期') DESC, id LIMIT 1",
        (owner,)).fetchone()
    if not row:
        raise HTTPException(404, {"code": "NO_ACCOUNT", "message": "当前用户名下没有可用账户"})
    return int(row["id"])


def _account_or_default(account_id: int | None) -> int:
    """显式传入账户时校验归属，未传入时使用默认账户，不回退到其他用户。"""
    if account_id is None:
        return _default_account()
    _own("accounts", account_id, uid(), "账户")
    return int(account_id)


def _row2dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _maybe_busy():
    if NOISE_RATE and random.random() < NOISE_RATE:
        raise HTTPException(503, {"code": "SYSTEM_BUSY", "message": "系统繁忙，请稍后重试"})


def _today_sum(user_id: int) -> Decimal:
    today = date.today().isoformat()
    rows = _conn.execute(
        "SELECT amount FROM transfers WHERE user_id=? AND status='done' AND substr(created_at,1,10)=?",
        (user_id, today),
    ).fetchall()
    return sum((D(r["amount"]) for r in rows), Decimal("0"))


# ---------------- 账户 ----------------

@app.get("/v1/health")
def health():
    return {"ok": True, "service": "mock-bank", "time": _now(), "noise": NOISE_RATE}


@app.post("/v1/admin/reset")
def admin_reset():
    """仅本地演示用：清库并重建种子数据，让剧本可以反复跑。

    真实银行没有这个接口；上线部署时把本路由删掉或加鉴权。
    """
    global _conn
    from backend.mock_bank import seed as _seed

    _conn.close()
    _seed.reset()
    _seed.seed()
    _conn = connect()
    init_db(_conn)
    return {"ok": True, "reset": True, "time": _now()}


@app.get("/v1/accounts")
def list_accounts():
    rows = _conn.execute("SELECT * FROM accounts WHERE user_id=?", (uid(),)).fetchall()
    return {"accounts": [_row2dict(r) for r in rows]}


@app.get("/v1/accounts/{account_id}/balance")
def balance(account_id: int):
    _own("accounts", account_id, uid(), "账户")
    r = _conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not r:
        raise HTTPException(404, {"code": "ACCOUNT_NOT_FOUND", "message": "账户不存在"})
    return {"account_id": account_id, "last4": r["last4"], "balance": r["balance"]}


# ---------------- 收款人 ----------------

@app.get("/v1/payees")
def all_payees():
    rows = _conn.execute("SELECT * FROM payees WHERE user_id=?", (uid(),)).fetchall()
    return {"payees": [_row2dict(r) for r in rows]}


@app.get("/v1/payees/search")
def search_payee(q: str = Query(..., min_length=1)):
    """模糊匹配：人名全等 > 关系匹配 > 编辑距离。返回 top-5 + 相似度。"""
    import difflib

    q = q.strip().strip("我的").strip()
    # 通讯录心智别名：口语里的"妈"要能落到"母亲"这个关系上
    ALIAS = {"妈": "母亲", "妈妈": "母亲", "老妈": "母亲", "娘": "母亲",
             "爸": "父亲", "爸爸": "父亲", "老爸": "父亲",
             "老婆": "妻子", "媳妇": "妻子", "老公": "丈夫",
             "房东": "房东", "老板": "房东"}
    q = ALIAS.get(q, q)
    rows = _conn.execute("SELECT * FROM payees WHERE user_id=?", (uid(),)).fetchall()
    out = []
    for r in rows:
        ratio = difflib.SequenceMatcher(None, q, r["name"]).ratio()
        score = ratio
        if q == r["name"]:
            score = 1.0
        elif q and r["relation"] and (q == r["relation"] or q in r["relation"]):
            score = max(score, 0.93)
        elif q in r["name"] or r["name"] in q:
            score = max(score, 0.88)
        if score >= 0.6:
            d = _row2dict(r)
            d["score"] = round(score, 3)
            out.append(d)
    out.sort(key=lambda x: -x["score"])
    return {"query": q, "candidates": out[:5]}


# ---------------- 转账 ----------------

@app.post("/v1/transfers")
def create_transfer(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
    _maybe_busy()
    if not idempotency_key:
        raise HTTPException(400, {"code": "IDEMPOTENCY_REQUIRED", "message": "写操作必须携带 Idempotency-Key"})

    hit = _conn.execute("SELECT * FROM transfers WHERE idem_key=?", (idempotency_key,)).fetchone()
    if hit:                                     # 幂等：返回首次结果
        # 注意：重放响应必须与首次响应结构一致，否则客户端重试时可能解析失败
        payee0 = _conn.execute("SELECT * FROM payees WHERE id=?", (hit["payee_id"],)).fetchone()
        return {
            "transfer_id": hit["id"],
            "status": hit["status"],
            "amount": hit["amount"],
            "payee_name": payee0["name"] if payee0 else "",
            "payee_last4": payee0["account_last4"] if payee0 else "",
            "memo": hit["memo"] or "",
            "created_at": hit["created_at"],
            "balance_after": None,
            "idempotent_replay": True,
        }

    account_id = int(payload["account_id"])
    amount = D(payload["amount"])
    payee_id = int(payload["payee_id"])
    memo = payload.get("memo") or ""

    # 归属校验必须在动钱之前：账户与收款人都得是本人的
    _own("accounts", account_id, uid(), "账户")
    _own("payees", payee_id, uid(), "收款人")

    if amount <= 0:
        raise HTTPException(400, {"code": "INVALID_AMOUNT", "message": "金额必须大于 0"})
    if amount > SINGLE_LIMIT:
        raise HTTPException(400, {"code": "LIMIT_EXCEEDED", "message": f"单笔限额 {mq(SINGLE_LIMIT)} 元"})
    if _today_sum(uid()) + amount > DAILY_LIMIT:
        raise HTTPException(400, {"code": "LIMIT_EXCEEDED", "message": f"日累计限额 {mq(DAILY_LIMIT)} 元"})

    acc = _conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    payee = _conn.execute("SELECT * FROM payees WHERE id=?", (payee_id,)).fetchone()
    if not acc or not payee:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "账户或收款人不存在"})
    if payee["blacklisted"]:
        raise HTTPException(403, {"code": "PAYEE_BLACKLISTED", "message": "收款人命中反诈黑名单"})
    if D(acc["balance"]) < amount:
        raise HTTPException(400, {"code": "INSUFFICIENT_FUNDS",
                                 "message": f"余额不足，可用余额 {mq(acc['balance'])} 元"})

    now = _now()
    _conn.execute("UPDATE accounts SET balance=? WHERE id=?", (mq(D(acc["balance"]) - amount), account_id))
    cur = _conn.execute(
        "INSERT INTO transfers(user_id,account_id,payee_id,amount,memo,kind,status,created_at,idem_key)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (uid(), account_id, payee_id, mq(amount), memo, "instant", "done", now, idempotency_key),
    )
    _conn.execute(
        "INSERT INTO transactions(account_id,ts,merchant,category,sub_category,amount,direction,city,note)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (account_id, now, f"转账-{payee['name']}", "转账", "转出", mq(amount), "out", "绵阳", memo),
    )
    _conn.commit()
    return {
        "transfer_id": cur.lastrowid,
        "status": "done",
        "amount": mq(amount),
        "payee_name": payee["name"],
        "payee_last4": payee["account_last4"],
        "memo": memo,
        "created_at": now,
        "balance_after": mq(D(acc["balance"]) - amount),
        "idempotent_replay": False,
    }


@app.post("/v1/transfers/schedule")
def schedule_transfer(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
    if not idempotency_key:
        raise HTTPException(400, {"code": "IDEMPOTENCY_REQUIRED", "message": "写操作必须携带 Idempotency-Key"})
    hit = _conn.execute("SELECT * FROM transfers WHERE idem_key=?", (idempotency_key,)).fetchone()
    if hit:
        d = _row2dict(hit)
        d["idempotent_replay"] = True
        return d

    amount = D(payload["amount"])
    payee = _conn.execute("SELECT * FROM payees WHERE id=?", (int(payload["payee_id"]),)).fetchone()
    if not payee:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "收款人不存在"})
    _own("accounts", int(payload["account_id"]), uid(), "账户")
    _own("payees", int(payload["payee_id"]), uid(), "收款人")
    if amount > SINGLE_LIMIT:
        raise HTTPException(400, {"code": "LIMIT_EXCEEDED", "message": f"单笔限额 {mq(SINGLE_LIMIT)} 元"})

    cur = _conn.execute(
        "INSERT INTO transfers(user_id,account_id,payee_id,amount,memo,kind,status,created_at,first_run_at,recurrence,idem_key)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (uid(), int(payload["account_id"]), int(payload["payee_id"]), mq(amount),
         payload.get("memo") or "", "scheduled", "pending", _now(),
         payload["first_run_at"], payload.get("recurrence"), idempotency_key),
    )
    _conn.commit()
    return {
        "transfer_id": cur.lastrowid,
        "status": "pending",
        "amount": mq(amount),
        "payee_name": payee["name"],
        "first_run_at": payload["first_run_at"],
        "recurrence": payload.get("recurrence"),
        "memo": payload.get("memo") or "",
    }


@app.post("/v1/transfers/collect")
def collect_aa(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
    """AA 收款：总价 / 人数，向上取整到分，差额计入最后一人。"""
    total = D(payload["total"])
    people = int(payload["people"])
    if people < 2:
        raise HTTPException(400, {"code": "INVALID_PEOPLE", "message": "AA 至少 2 人"})
    if total <= 0:
        raise HTTPException(400, {"code": "INVALID_AMOUNT", "message": "金额必须大于 0"})

    per = (total / people).quantize(Decimal("0.01"))
    remainder = total - per * people
    shares = [mq(per)] * people
    shares[-1] = mq(per + remainder)
    req_id = f"AA{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"
    return {
        "request_id": req_id,
        "total": mq(total),
        "people": people,
        "per_person": mq(per),
        "shares": shares,
        "status": "sent",
        "note": payload.get("memo") or "AA 收款",
    }


@app.get("/v1/transfers")
def list_transfers(limit: int = 10):
    rows = _conn.execute(
        "SELECT t.*, p.name AS payee_name FROM transfers t LEFT JOIN payees p ON p.id=t.payee_id"
        " WHERE t.user_id=? ORDER BY t.id DESC LIMIT ?", (uid(), limit)).fetchall()
    return {"transfers": [_row2dict(r) for r in rows]}


# ---------------- 账单 ----------------

_AGG_COLUMNS = {"category": "category", "merchant": "merchant"}


def _agg_out(col: str, account_id: int, d0: str, d1: str) -> dict[str, tuple[int, Decimal]]:
    """按列聚合流出金额（笔数, 金额合计）。

    金额以 TEXT 存储，此处使用 Decimal 累加。若改为 SQL 的 SUM(CAST(... AS REAL))，
    浮点分位误差会随交易数量累积。
    """
    sql = ("SELECT " + _AGG_COLUMNS[col] + " AS k, amount FROM transactions"
           " WHERE account_id=? AND direction='out' AND substr(ts,1,10) BETWEEN ? AND ?")
    rows = _conn.execute(sql, (account_id, d0, d1)).fetchall()
    agg: dict[str, tuple[int, Decimal]] = {}
    for r in rows:
        k = r["k"] or ""
        cnt, s = agg.get(k, (0, Decimal("0")))
        agg[k] = (cnt + 1, s + D(r["amount"]))
    return agg


@app.get("/v1/bills/summary")
def bill_summary(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"),
                 account_id: int | None = None):
    account_id = _account_or_default(account_id)
    cat_agg = _agg_out("category", account_id, date_from, date_to)
    cats = [{"category": k, "count": c, "amount": mq(v)} for k, (c, v) in cat_agg.items()]
    cats.sort(key=lambda x: -D(x["amount"]))
    total = sum((D(c["amount"]) for c in cats), Decimal("0"))
    for c in cats:
        c["ratio"] = round(float(D(c["amount"]) / total * 100), 1) if total else 0.0

    # 环比：同口径对比上个月相同日期区间（10-01~10-27 vs 09-01~09-27）
    from datetime import date as _d

    def _shift_month(d0: _d, delta: int) -> _d:
        y, m = d0.year, d0.month + delta
        while m > 12:
            y, m = y + 1, m - 12
        while m < 1:
            y, m = y - 1, m + 12
        last = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        return _d(y, m, min(d0.day, last))

    pf = _shift_month(_d.fromisoformat(date_from), -1)
    pt = _shift_month(_d.fromisoformat(date_to), -1)
    prev_agg = _agg_out("category", account_id, pf.isoformat(), pt.isoformat())
    prev_total = sum((v for _c, v in prev_agg.values()), Decimal("0"))
    mom = None
    if prev_total > 0:
        mom = round(float((total - prev_total) / prev_total * 100), 1)

    merc_agg = _agg_out("merchant", account_id, date_from, date_to)
    top = sorted(merc_agg.items(), key=lambda kv: -kv[1][1])[:3]
    return {
        "range": f"{date_from}~{date_to}",
        "total_out": mq(total),
        "count": sum(c["count"] for c in cats),
        "categories": cats,
        "mom_pct": mom,
        "top_merchants": [{"merchant": k, "amount": mq(v[1])} for k, v in top],
    }


@app.get("/v1/bills/transactions")
def bill_transactions(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"),
                      category: str | None = None, account_id: int | None = None, limit: int = 50):
    account_id = _account_or_default(account_id)
    sql = ("SELECT * FROM transactions WHERE account_id=? AND substr(ts,1,10) BETWEEN ? AND ?")
    args: list[Any] = [account_id, date_from, date_to]
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    rows = _conn.execute(sql, args).fetchall()
    return {"transactions": [_row2dict(r) for r in rows]}


@app.get("/v1/bills/anomalies")
def bill_anomalies(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"),
                   account_id: int | None = None):
    account_id = _account_or_default(account_id)
    """异常识别全部在服务端按规则算，模型只负责解释，不负责判定。"""
    rows = _conn.execute(
        "SELECT * FROM transactions WHERE account_id=? AND direction='out'"
        " AND substr(ts,1,10) BETWEEN ? AND ? ORDER BY ts", (account_id, date_from, date_to)).fetchall()
    txs = [_row2dict(r) for r in rows]
    found: list[dict] = []
    seen: set[int] = set()

    # 1) 品类统计离群（> 均值 + 3σ）
    by_cat: dict[str, list[Decimal]] = {}
    for t in txs:
        by_cat.setdefault(t["category"], []).append(D(t["amount"]))
    stats = {}
    for c, vals in by_cat.items():
        if len(vals) >= 5:
            mean = sum(vals, Decimal("0")) / len(vals)
            var = sum(((v - mean) ** 2 for v in vals), Decimal("0")) / len(vals)
            sigma = float(var) ** 0.5
            stats[c] = (mean, sigma)
    for t in txs:
        st = stats.get(t["category"])
        if st and float(D(t["amount"])) > float(st[0]) + 3 * st[1] and D(t["amount"]) > 300:
            found.append({"tx": t, "rule": "STAT_OUTLIER",
                          "reason": f"{t['category']}类平均 {mq(st[0])} 元，本笔 {t['amount']} 元，显著偏高"})
            seen.add(t["id"])

    # 2) 凌晨连刷
    night = [t for t in txs if 0 <= int(t["ts"][11:13]) < 6]
    for i in range(len(night) - 2):
        g = night[i:i + 3]
        t0 = datetime.fromisoformat(g[0]["ts"])
        t2 = datetime.fromisoformat(g[2]["ts"])
        if (t2 - t0) <= timedelta(minutes=30) and all(x["id"] not in seen for x in g):
            for x in g:
                found.append({"tx": x, "rule": "NIGHT_BURST",
                              "reason": "凌晨 0-6 点 30 分钟内连续 3 笔，符合盗刷特征"})
                seen.add(x["id"])

    # 3) 同商户等额重复扣款
    grp: dict[tuple, list] = {}
    for t in txs:
        grp.setdefault((t["merchant"], t["amount"]), []).append(t)
    for (m, a), items in grp.items():
        if len(items) >= 2 and items[0]["id"] not in seen:
            d0 = datetime.fromisoformat(items[0]["ts"])
            d1 = datetime.fromisoformat(items[1]["ts"])
            # 同日或隔日（按自然日）+ 金额 >= 50 才算可疑重复；小额日常消费不误报
            if (d1.date() - d0.date()).days <= 1 and D(a) >= 50:
                for x in items[:2]:
                    found.append({"tx": x, "rule": "DUPLICATE",
                                  "reason": f"{m} 同日或隔日同金额 {a} 元扣款 2 次，疑似重复扣款"})
                    seen.add(x["id"])

    # 4) 整数大额
    for t in txs:
        if t["id"] not in seen and D(t["amount"]) > 3000 and D(t["amount"]) % 1000 == 0:
            found.append({"tx": t, "rule": "ROUND_LARGE",
                          "reason": f"整数大额 {t['amount']} 元，常见于异常资金流出"})
            seen.add(t["id"])

    # 5) 异地消费
    for t in txs:
        if t["id"] not in seen and t["city"] and t["city"] != "绵阳":
            found.append({"tx": t, "rule": "REMOTE_CITY",
                          "reason": f"在{t['city']}消费，与常用城市绵阳不一致"})
            seen.add(t["id"])

    found.sort(key=lambda x: x["tx"]["ts"])
    return {"count": len(found), "anomalies": [
        {"ts": f["tx"]["ts"], "merchant": f["tx"]["merchant"], "amount": f["tx"]["amount"],
         "category": f["tx"]["category"], "rule": f["rule"], "reason": f["reason"]} for f in found
    ]}


# ---------------- 理财 ----------------

RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
CUSTOMER_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}


@app.get("/v1/wealth/products")
def wealth_products(risk_max: str = "R3", term_max: int = 9999, keyword: str | None = None):
    rows = _conn.execute("SELECT * FROM products").fetchall()
    out = []
    for r in rows:
        if RISK_ORDER[r["risk"]] > RISK_ORDER.get(risk_max, 3):
            continue
        if r["term_days"] > term_max:
            continue
        if keyword and keyword not in r["name"] and keyword not in (r["tag"] or ""):
            continue
        out.append(_row2dict(r))
    return {"products": out}


@app.post("/v1/wealth/risk-profile")
def risk_profile(payload: dict = Body(default={})):
    """问卷 → 客户风险等级。演示用固定 3 题。"""
    answers = payload.get("answers") or [3, 3, 3]
    score = sum(answers)
    level = "C1" if score <= 4 else "C2" if score <= 7 else "C3" if score <= 10 else "C4" if score <= 13 else "C5"
    _conn.execute("UPDATE users SET risk_level=? WHERE id=1", (level,))
    _conn.commit()
    return {"level": level, "score": score, "answers": answers,
            "desc": {"C1": "保守型", "C2": "稳健型", "C3": "平衡型", "C4": "成长型", "C5": "进取型"}[level]}


@app.get("/v1/wealth/holdings")
def holdings():
    rows = _conn.execute("SELECT h.*, p.name, p.annual_rate FROM holdings h JOIN products p ON p.id=h.product_id"
                         " WHERE h.user_id=? ORDER BY h.id DESC", (uid(),)).fetchall()
    return {"holdings": [_row2dict(r) for r in rows]}


def _idem(scope: str, key: str | None, handler) -> dict:
    """统一幂等处理：相同 Idempotency-Key 只执行一次，重试返回首次结果。

    理财申购、赎回和礼物下单都需要消费网关传入的幂等请求头，避免超时重试产生重复订单。
    """
    if not key:
        raise HTTPException(400, {"code": "IDEMPOTENCY_REQUIRED",
                                  "message": "写操作必须携带 Idempotency-Key"})
    full = f"{scope}:{key}"
    hit = _conn.execute("SELECT response FROM idem_keys WHERE key=?", (full,)).fetchone()
    if hit:
        return {**json.loads(hit["response"]), "idempotent_replay": True}
    resp = handler()
    try:
        _conn.execute("INSERT INTO idem_keys(key,scope,response,created_at) VALUES(?,?,?,?)",
                      (full, scope, json.dumps(resp, ensure_ascii=False), _now()))
        _conn.commit()
    except sqlite3.IntegrityError:
        # 并发同 key：另一路已经写进去了，忽略即可（首次结果已在库里）
        _conn.rollback()
    return resp


@app.post("/v1/wealth/orders")
def wealth_order(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
  def _do() -> dict:
    product = _conn.execute("SELECT * FROM products WHERE id=?", (payload["product_id"],)).fetchone()
    if not product:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND", "message": "产品不存在"})
    user = _conn.execute("SELECT * FROM users WHERE id=?", (uid(),)).fetchone()
    if not user:
        raise HTTPException(404, {"code": "USER_NOT_FOUND", "message": "用户不存在"})
    if RISK_ORDER[product["risk"]] > CUSTOMER_ORDER[user["risk_level"]]:
        raise HTTPException(400, {"code": "UNSUITABLE",
                                 "message": f"该产品风险等级 {product['risk']} 高于您的承受能力 {user['risk_level']}"})
    amount = D(payload["amount"])
    if amount < D(product["min_amount"]):
        raise HTTPException(400, {"code": "BELOW_MIN",
                                 "message": f"起购金额 {product['min_amount']} 元"})
    account_id = _default_account()
    acc = _conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if D(acc["balance"]) < amount:
        raise HTTPException(400, {"code": "INSUFFICIENT_FUNDS",
                                 "message": f"余额不足，可用 {mq(acc['balance'])} 元"})
    _conn.execute("UPDATE accounts SET balance=? WHERE id=?",
                  (mq(D(acc["balance"]) - amount), account_id))
    cur = _conn.execute("INSERT INTO holdings(user_id,product_id,amount,ts) VALUES(?,?,?,?)",
                        (uid(), product["id"], mq(amount), _now()))
    _conn.commit()
    return {"order_id": cur.lastrowid, "product": product["name"], "amount": mq(amount),
            "status": "confirmed_pending", "confirm_note": "T+1 确认份额，份额以确认结果为准",
            "annual_rate": product["annual_rate"], "redeem_rule": product["redeem_rule"]}
  return _idem("wealth.order", idempotency_key, _do)


@app.post("/v1/wealth/redeem")
def wealth_redeem(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
  def _do() -> dict:
    h = _conn.execute("SELECT h.*, p.name, p.redeem_rule FROM holdings h JOIN products p ON p.id=h.product_id"
                      " WHERE h.id=?", (int(payload["holding_id"]),)).fetchone()
    if not h:
        raise HTTPException(404, {"code": "HOLDING_NOT_FOUND", "message": "持仓不存在"})
    if int(h["user_id"]) != uid():
        raise HTTPException(403, {"code": "FORBIDDEN", "message": "持仓不属于当前用户"})
    amount = D(payload.get("amount") or h["amount"])
    if amount > D(h["amount"]):
        raise HTTPException(400, {"code": "EXCEED_HOLDING", "message": "赎回金额超过持仓"})
    account_id = _default_account()
    acc = _conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    _conn.execute("UPDATE accounts SET balance=? WHERE id=?",
                  (mq(D(acc["balance"]) + amount), account_id))
    _conn.execute("UPDATE holdings SET amount=? WHERE id=?", (mq(D(h["amount"]) - amount), int(payload["holding_id"])))
    _conn.commit()
    return {"holding_id": int(payload["holding_id"]), "product": h["name"], "amount": mq(amount),
            "status": "redeeming", "arrive_note": "预计 T+1 到账", "redeem_rule": h["redeem_rule"]}
  return _idem("wealth.redeem", idempotency_key, _do)


# ---------------- 卡片 ----------------

@app.get("/v1/cards")
def list_cards():
    rows = _conn.execute("SELECT * FROM cards WHERE user_id=?", (uid(),)).fetchall()
    return {"cards": [_row2dict(r) for r in rows]}


@app.post("/v1/cards/apply")
def card_apply(payload: dict = Body(default={})):
    return {"apply_id": f"CA{datetime.now().strftime('%Y%m%d%H%M%S')}", "status": "submitted",
            "card_type": payload.get("card_type", "标准白金卡"),
            "note": "申请已提交，预计 3 个工作日审批，制卡后邮寄至预留地址"}


@app.patch("/v1/cards/{card_id}/limit")
def card_limit(card_id: int, payload: dict = Body(...)):
    _own("cards", card_id, uid(), "卡片")
    r = _conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not r:
        raise HTTPException(404, {"code": "CARD_NOT_FOUND", "message": "卡片不存在"})
    new_limit = D(payload["daily_limit"])
    if new_limit > D("100000"):
        return {"card_id": card_id, "status": "pending_review",
                "ticket": f"TK{datetime.now().strftime('%m%d%H%M%S')}",
                "note": f"调整至 {mq(new_limit)} 元需人工审批，预计 1 个工作日内答复"}
    _conn.execute("UPDATE cards SET daily_limit=? WHERE id=?", (mq(new_limit), card_id))
    _conn.commit()
    return {"card_id": card_id, "status": "done", "daily_limit": mq(new_limit), "note": "额度已调整，即时生效"}


@app.post("/v1/cards/{card_id}/freeze")
def card_freeze(card_id: int, payload: dict = Body(default={})):
    _own("cards", card_id, uid(), "卡片")
    frozen = 1 if payload.get("freeze", True) else 0
    _conn.execute("UPDATE cards SET frozen=?, status=? WHERE id=?",
                  (frozen, "frozen" if frozen else "active", card_id))
    _conn.commit()
    return {"card_id": card_id, "frozen": bool(frozen),
            "status": "frozen" if frozen else "active",
            "note": "临时冻结已生效，可随时解冻" if frozen else "已解除冻结，可正常使用"}


@app.post("/v1/cards/{card_id}/loss")
def card_loss(card_id: int):
    _own("cards", card_id, uid(), "卡片")
    _conn.execute("UPDATE cards SET status='lost', frozen=1 WHERE id=?", (card_id,))
    _conn.commit()
    return {"card_id": card_id, "status": "lost", "irreversible": True,
            "ticket": f"LS{datetime.now().strftime('%m%d%H%M%S')}",
            "note": "原卡即时失效，补卡费 15 元，新卡 5 个工作日内寄出"}


# ---------------- 订阅 ----------------

@app.get("/v1/subscriptions")
def subs():
    rows = _conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND status='active'",
                         (uid(),)).fetchall()
    items, annual = [], Decimal("0")
    for r in rows:
        per_year = {"月": D(r["amount"]) * 12, "季": D(r["amount"]) * 4, "年": D(r["amount"])}[r["cycle"]]
        annual += per_year
        d = _row2dict(r)
        d["annual_cost"] = mq(per_year)
        items.append(d)
    return {"count": len(items), "annual_total": mq(annual), "items": items}


@app.post("/v1/subscriptions/{sub_id}/cancel")
def sub_cancel(sub_id: int):
    _own("subscriptions", sub_id, uid(), "订阅")
    r = _conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    if not r:
        raise HTTPException(404, {"code": "SUB_NOT_FOUND", "message": "订阅不存在"})
    _conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (sub_id,))
    _conn.commit()
    return {"sub_id": sub_id, "merchant": r["merchant"], "status": "cancelled",
            "effective": "本计费周期结束时停止扣款，当前周期内权益不受影响"}


# ---------------- 风控辅助 ----------------

@app.get("/v1/risk/stats")
def risk_stats():
    one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    velocity = _conn.execute(
        "SELECT COUNT(*) c FROM transfers WHERE user_id=? AND kind IN ('instant','scheduled','collect')"
        " AND created_at>=?", (uid(), one_hour_ago)).fetchone()["c"]
    today = date.today().isoformat()
    daily = _conn.execute(
        "SELECT COUNT(*) c FROM transfers WHERE user_id=? AND substr(created_at,1,10)=?", (uid(), today)
    ).fetchone()["c"]
    known = _conn.execute(
        "SELECT DISTINCT payee_id FROM transfers WHERE user_id=?", (uid(),)).fetchall()
    recent = _conn.execute(
        "SELECT DISTINCT payee_id FROM transfers WHERE user_id=? AND created_at>=?",
        (uid(), (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    user = _conn.execute("SELECT * FROM users WHERE id=?", (uid(),)).fetchone()
    return {"velocity_1h": velocity, "daily_count": daily,
            "known_payees": [r["payee_id"] for r in known],
            "recent_payees": [r["payee_id"] for r in recent],
            "segment": user["segment"] if user else "普通",
            "risk_level": user["risk_level"] if user else "C3"}


# ---------------- 资金锁定 / 三方 ----------------

@app.post("/v1/holds")
def create_hold(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
    """锁定资金。该操作改变账户状态，因此必须携带幂等键。

    如果缺少幂等处理，网络超时后的重试会重复锁定资金，导致可用余额异常减少。
    """
    if not idempotency_key:
        raise HTTPException(400, {"code": "IDEMPOTENCY_REQUIRED",
                                  "message": "写操作必须携带 Idempotency-Key"})
    hit = _conn.execute("SELECT * FROM holds WHERE idem_key=?", (idempotency_key,)).fetchone()
    if hit:
        return {"hold_id": hit["id"], "amount": hit["amount"], "status": hit["status"],
                "expire_at": hit["expire_at"], "idempotent_replay": True}
    cur = _conn.execute(
        "INSERT INTO holds(user_id,amount,reason,status,expire_at,created_at,idem_key)"
        " VALUES(?,?,?,?,?,?,?)",
        (uid(), mq(D(payload["amount"])), payload.get("reason", "跨场景资金锁定"),
         "held", payload.get("expire_at"), _now(), idempotency_key))
    _conn.commit()
    return {"hold_id": cur.lastrowid, "amount": mq(D(payload["amount"])), "status": "held",
            "expire_at": payload.get("expire_at"), "idempotent_replay": False}


@app.post("/v1/holds/{hold_id}/release")
def release_hold(hold_id: int, idempotency_key: str | None = Header(default=None)):
    _own("holds", hold_id, uid(), "资金锁定")
    _conn.execute("UPDATE holds SET status='released' WHERE id=?", (hold_id,))
    _conn.commit()
    r = _conn.execute("SELECT * FROM holds WHERE id=?", (hold_id,)).fetchone()
    return {"hold_id": hold_id, "status": "released", "amount": r["amount"] if r else "0.00"}


GIFTS = [
    {"gift_id": "G01", "name": "康乃馨花束·温柔粉", "amount": "299.00", "vendor": "花点时间"},
    {"gift_id": "G02", "name": "玫瑰混搭花束·晨露", "amount": "399.00", "vendor": "花点时间"},
    {"gift_id": "G03", "name": "生日蛋糕·6寸轻乳酪", "amount": "228.00", "vendor": "幸福西饼"},
]


@app.get("/v1/partner/gift/recommend")
def gift_recommend(budget: str = "800"):
    b = D(budget)
    out = [g for g in GIFTS if D(g["amount"]) <= b]
    return {"budget": mq(b), "gifts": out}


@app.post("/v1/partner/gift/order")
def gift_order(payload: dict = Body(...), idempotency_key: str | None = Header(default=None)):
  def _do() -> dict:
    g = next((x for x in GIFTS if x["gift_id"] == payload["gift_id"]), None)
    if not g:
        raise HTTPException(404, {"code": "GIFT_NOT_FOUND", "message": "商品不存在"})
    cur = _conn.execute(
        "INSERT INTO gift_orders(user_id,gift_id,name,amount,status,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (uid(), g["gift_id"], g["name"], g["amount"], "created", _now()))
    _conn.commit()
    return {"order_id": cur.lastrowid, "gift_id": g["gift_id"], "name": g["name"],
            "amount": g["amount"], "vendor": g["vendor"], "status": "created",
            "deliver_at": payload.get("deliver_at")}
  return _idem("gift.order", idempotency_key, _do)


@app.get("/v1/partner/gift/orders")
def gift_orders():
    rows = _conn.execute("SELECT * FROM gift_orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
                         (uid(),)).fetchall()
    return {"orders": [_row2dict(r) for r in rows]}
