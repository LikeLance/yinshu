"""造数：12 个月模拟流水 + 通讯录 + 理财货架 + 订阅。

刻意保留"真实世界的不干净"：
- 同名收款人（两个李慧）用来演示消歧；
- 黑名单收款人、跨境收款人（用来演示硬规则）；
- 6 类可被规则捕捉的异常交易；
- 订阅涨价、退款、月初房租等生活化模式。

用法：python -m backend.mock_bank.seed --reset
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from backend.core.config import BANK_DB
from backend.mock_bank.db import D, connect, init_db, mq

RND = random.Random(42)
TODAY = date.today()
NOW = datetime.now()

MERCHANTS: list[tuple[str, str, str, int, int, float, str]] = [
    # (商户, 大类, 子类, 最小, 最大, 权重, 城市)
    ("星巴克", "餐饮", "咖啡", 32, 48, 4, "绵阳"),
    ("美团外卖", "餐饮", "外卖", 22, 62, 10, "绵阳"),
    ("海底捞", "餐饮", "正餐", 180, 320, 1, "绵阳"),
    ("兰州拉面", "餐饮", "快餐", 16, 28, 5, "绵阳"),
    ("瑞幸咖啡", "餐饮", "咖啡", 15, 26, 6, "绵阳"),
    ("滴滴出行", "交通", "打车", 12, 46, 6, "绵阳"),
    ("绵阳公交", "交通", "公交", 2, 2, 4, "绵阳"),
    ("中石油加油站", "交通", "加油", 300, 450, 1, "绵阳"),
    ("京东", "购物", "电商", 99, 799, 3, "绵阳"),
    ("淘宝", "购物", "电商", 39, 469, 5, "绵阳"),
    ("优衣库", "购物", "服饰", 199, 599, 1, "绵阳"),
    ("永辉超市", "购物", "超市", 68, 298, 4, "绵阳"),
    ("万达影城", "娱乐", "电影", 45, 90, 2, "绵阳"),
    ("Steam", "娱乐", "游戏", 68, 198, 1, "绵阳"),
    ("国家电网", "生活缴费", "电费", 120, 260, 1, "绵阳"),
    ("中国移动", "生活缴费", "话费", 58, 128, 1, "绵阳"),
    ("绵阳市中心医院", "医疗", "门诊", 80, 450, 1, "绵阳"),
    ("益丰大药房", "医疗", "药品", 28, 120, 1, "绵阳"),
]

PRODUCTS = [
    ("P01", "银枢活期+", "R1", 0, "0.01", "2.15", "现金管理", "T+0 实时赎回，单日快取 1 万元", "随时取"),
    ("P02", "稳健增利 30 天", "R1", 30, "1000.00", "2.65", "短债", "到期自动续期，可提前赎回", "短钱"),
    ("P03", "安心债基 A", "R2", 0, "100.00", "3.42", "债券型", "T+1 赎回，无赎回费", "低波动"),
    ("P04", "月月盈 90 天", "R2", 90, "5000.00", "3.10", "固收+", "封闭 90 天，到期一次性兑付", "闲钱"),
    ("P05", "中证红利指数增强", "R3", 0, "1000.00", "5.80", "指数型", "T+1 赎回，持有 7 天内赎回费 1.5%", "长期"),
    ("P06", "均衡配置混合", "R3", 0, "1000.00", "6.25", "混合型", "T+2 赎回", "均衡"),
    ("P07", "科技成长混合", "R4", 0, "1000.00", "9.40", "混合型", "T+2 赎回，波动较大", "高波动"),
    ("P08", "新能源主题股票", "R5", 0, "1000.00", "12.60", "股票型", "T+3 赎回，历史最大回撤 28%", "高风险"),
    ("P09", "黄金 ETF 联接", "R3", 0, "100.00", "7.10", "商品型", "T+1 赎回", "抗通胀"),
    ("P10", "同业存单指数 7 天", "R1", 7, "1.00", "2.05", "货币型", "T+0 快取 1 万", "零钱"),
    ("P11", "养老目标 2035", "R3", 1095, "1000.00", "4.80", "FOF", "持有期 3 年，不可提前赎回", "长钱"),
    ("P12", "美元债 QDII", "R4", 180, "5000.00", "5.60", "QDII", "T+3 赎回，汇率波动", "海外"),
    ("P13", "现金管理日日结", "R1", 0, "0.01", "1.95", "货币型", "T+0 实时赎回 5 万", "活钱"),
    ("P14", "量化对冲精选", "R4", 365, "10000.00", "8.20", "对冲", "封闭 1 年", "机构"),
]


def _ts(d: date, hour: int, minute: int) -> str:
    return f"{d.isoformat()} {hour:02d}:{minute:02d}:00"


def reset() -> None:
    for p in (BANK_DB, BANK_DB.with_suffix(".db-wal"), BANK_DB.with_suffix(".db-shm")):
        if p.exists():
            p.unlink()


def seed() -> None:
    conn = connect()
    init_db(conn)
    cur = conn.cursor()
    for t in ("users", "accounts", "cards", "payees", "transactions", "products",
              "holdings", "subscriptions", "transfers", "holds", "gift_orders"):
        cur.execute(f"DELETE FROM {t}")

    # ---------- 用户 / 账户 / 卡 ----------
    cur.execute("INSERT INTO users(id,name,segment,risk_level,phone,birthday) VALUES(1,'张明','普通','C3','138****6688','1992-05-18')")
    cur.execute("INSERT INTO accounts(id,user_id,kind,last4,balance) VALUES(1,1,'活期','3921','38600.00')")
    cur.execute("INSERT INTO accounts(id,user_id,kind,last4,balance) VALUES(2,1,'定期','7788','100000.00')")
    cur.execute("INSERT INTO cards(id,user_id,account_id,last4,brand,daily_limit,status,frozen)"
                " VALUES(1,1,1,'6621','银联白金','50000.00','active',0)")
    cur.execute("INSERT INTO cards(id,user_id,account_id,last4,brand,daily_limit,status,frozen)"
                " VALUES(2,1,1,'3301','银联金卡','20000.00','active',0)")

    # 第二个客户：用来验证"多租户隔离"（身份从 X-User-Id 来，不靠 ?user_id= 猜）
    cur.execute("INSERT INTO users(id,name,segment,risk_level,phone,birthday)"
                " VALUES(2,'李雷','普通','C2','139****0001','1995-08-08')")
    cur.execute("INSERT INTO accounts(id,user_id,kind,last4,balance)"
                " VALUES(3,2,'活期','5566','12000.00')")
    cur.execute("INSERT INTO cards(id,user_id,account_id,last4,brand,daily_limit,status,frozen)"
                " VALUES(3,2,3,'9900','银联普卡','10000.00','active',0)")

    # ---------- 通讯录（含同名、黑名单、跨境） ----------
    payees = [
        (1, "李慧", "138****3921", "招商银行", "3921", "母亲", "绵阳", 0),
        (2, "李慧", "139****8842", "建设银行", "8842", "同事", "绵阳", 0),   # 同名干扰
        (3, "张建国", "137****5566", "工商银行", "5566", "父亲", "绵阳", 0),
        (4, "王强", "135****2233", "农业银行", "2233", "房东", "绵阳", 0),
        (5, "陈晓", "136****7788", "招商银行", "7788", "朋友", "绵阳", 0),
        (6, "刘洋", "131****9900", "邮储银行", "9900", "朋友", "绵阳", 1),   # 黑名单
        (7, "赵敏", "852-****1234", "HSBC HK", "1234", "同事", "香港", 0),  # 跨境（见下方 cross_border）
    ]
    # 客户 2 自己的收款人（id=8）。注意：它**不能**出现在客户 1 的通讯录里，
    # 反之亦然——隔离测试就是靠这个断言的。
    cur.execute("INSERT INTO payees(id,user_id,name,phone,bank,account_last4,relation,"
                "city,blacklisted,cross_border,created_at) VALUES(8,2,'韩梅梅','137****0002',"
                "'中国银行','0002','同事','绵阳',0,0,'2024-03-01 10:00:00')")
    for pid, name, phone, bank, last4, rel, city, bl in payees:
        cur.execute("INSERT INTO payees(id,user_id,name,phone,bank,account_last4,relation,"
                    "city,blacklisted,cross_border,created_at) VALUES(?,1,?,?,?,?,?,?,?,?,?)",
                    (pid, name, phone, bank, last4, rel, city, bl,
                     1 if pid == 7 else 0, "2024-03-01 10:00:00"))

    # ---------- 理财货架 ----------
    for p in PRODUCTS:
        cur.execute("INSERT INTO products(id,name,risk,term_days,min_amount,annual_rate,category,redeem_rule,tag)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", p)
    cur.execute("INSERT INTO holdings(user_id,product_id,amount,ts) VALUES(1,'P03','12000.00',?)",
                (_ts(TODAY - timedelta(days=95), 10, 22),))
    cur.execute("INSERT INTO holdings(user_id,product_id,amount,ts) VALUES(1,'P01','5000.00',?)",
                (_ts(TODAY - timedelta(days=30), 15, 8),))

    # ---------- 订阅（含涨价） ----------
    subs = [
        (1, "腾讯视频VIP", "30.00", "月", "2023-06-12", "近期由 25 元上调至 30 元"),
        (1, "网易云音乐黑胶", "15.00", "月", "2022-11-03", ""),
        (1, "iCloud 200G", "6.00", "月", "2021-08-20", ""),
        (1, "WPS 超级会员", "89.00", "年", "2024-01-15", ""),
        (1, "乐刻健身房", "199.00", "月", "2024-09-01", "近 3 个月无到店记录"),
        (1, "番茄小说", "12.00", "月", "2025-02-11", ""),
    ]
    for i, (uid, m, amt, cyc, first, note) in enumerate(subs, start=1):
        cur.execute("INSERT INTO subscriptions(id,user_id,merchant,amount,cycle,first_charge,last_charge,status,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (i, uid, m, amt, cyc, first, _ts(TODAY - timedelta(days=RND.randint(3, 20)), 9, 0), "active", note))

    # ---------- 12 个月流水 ----------
    rows: list[tuple] = []

    def add(d: date, hour: int, minute: int, merchant: str, cat: str, sub: str,
            amount: Decimal | str, direction: str, city: str = "绵阳", note: str = "") -> None:
        if d > TODAY:
            return
        rows.append((1, _ts(d, hour, minute), merchant, cat, sub, mq(amount), direction, city, note))

    start = (TODAY.replace(day=1) - timedelta(days=335)).replace(day=1)
    weights = [m[5] for m in MERCHANTS]

    d = start
    while d <= TODAY:
        # 固定项
        if d.day == 1:
            add(d, 9, 5, "转账-王强", "转账", "房租", "2500.00", "out", "绵阳", "10月房租")
        if d.day == 10:
            add(d, 10, 12, "工资", "收入", "工资", "8500.00", "in")
        # 订阅扣款
        if d.day == 12:
            add(d, 9, 0, "腾讯视频VIP", "订阅服务", "视频会员", "30.00", "out")
        if d.day == 3:
            add(d, 9, 0, "网易云音乐黑胶", "订阅服务", "音乐会员", "15.00", "out")
        if d.day == 20:
            add(d, 9, 0, "iCloud 200G", "订阅服务", "云存储", "6.00", "out")
        if d.day == 1:
            add(d, 9, 8, "乐刻健身房", "订阅服务", "健身", "199.00", "out")
        # 日常消费：每天 1~4 笔
        n = RND.choices([0, 1, 2, 3, 4], weights=[1, 3, 5, 4, 1])[0]
        for _ in range(n):
            m = RND.choices(MERCHANTS, weights=weights)[0]
            amt = D(RND.randint(m[3], m[4]))
            add(d, RND.randint(7, 22), RND.randint(0, 59), m[0], m[1], m[2], amt, "out", m[6])
        d += timedelta(days=1)

    # ---------- 注入 6 类异常（均落在近 40 天内，保证任一"近一月/近半年"报告都能看到） ----------
    base = TODAY - timedelta(days=12)
    # ① 凌晨连刷 3 笔（30 分钟内）
    add(base, 2, 11, "星耀互娱", "娱乐", "游戏充值", "198.00", "out")
    add(base, 2, 19, "星耀互娱", "娱乐", "游戏充值", "298.00", "out")
    add(base, 2, 26, "星耀互娱", "娱乐", "游戏充值", "398.00", "out")
    # ② 同商户等额重复扣款
    dup_day = TODAY - timedelta(days=6)
    add(dup_day, 12, 30, "美团外卖", "餐饮", "外卖", "268.00", "out")
    add(dup_day + timedelta(days=1), 12, 34, "美团外卖", "餐饮", "外卖", "268.00", "out")
    # ③ 整数大额
    add(TODAY - timedelta(days=8), 15, 20, "鼎盛商贸", "购物", "大额", "5000.00", "out")
    # ④ 异地消费
    add(TODAY - timedelta(days=4), 19, 40, "成都·蜀大侠火锅", "餐饮", "正餐", "460.00", "out", "成都")
    # ⑤ 品类离群（餐饮均值约 60-80 元）
    add(TODAY - timedelta(days=9), 20, 5, "绵州国际酒店", "餐饮", "宴请", "1580.00", "out")
    # ⑥ 退款（扰乱项，考验"不把退款算成消费"）
    add(TODAY - timedelta(days=3), 11, 2, "京东退款", "购物", "退款", "299.00", "in", "绵阳", "退货退款")

    rows.sort(key=lambda r: r[1])
    cur.executemany(
        "INSERT INTO transactions(account_id,ts,merchant,category,sub_category,amount,direction,city,note)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)

    # ---------- 历史转账（用于区分"白名单收款人"与"新收款人"） ----------
    hist = [
        (1, "800.00", "生活费", TODAY - timedelta(days=25)),
        (1, "600.00", "买菜", TODAY - timedelta(days=58)),
        (3, "1200.00", "过节", TODAY - timedelta(days=72)),
        (4, "2500.00", "房租", TODAY - timedelta(days=35)),
    ]
    for payee_id, amt, memo, day in hist:
        cur.execute("INSERT INTO transfers(user_id,account_id,payee_id,amount,memo,kind,status,created_at,idem_key)"
                    " VALUES(1,1,?,?,?,'instant','done',?,?)",
                    (payee_id, amt, memo, _ts(day, 10, 30), f"seed-{payee_id}-{day.isoformat()}"))

    conn.commit()
    total = cur.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    print(f"[seed] 用户 1 / 账户 2 / 卡 2 / 收款人 {len(payees)} / 理财 {len(PRODUCTS)} / "
          f"订阅 {len(subs)} / 流水 {total} 条 / 历史转账 {len(hist)} 笔")
    print(f"[seed] 数据文件：{BANK_DB}")
    conn.close()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    if BANK_DB.exists():
        c = connect()
        n = c.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
        c.close()
        if n and "--reset" not in sys.argv:
            print(f"[seed] 已有 {n} 条流水，跳过。需重建请加 --reset")
            sys.exit(0)
    seed()
