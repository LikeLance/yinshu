"""工具注册表：所有能"动钱"的能力都必须在这里登记。

网关只认这张表——不在表里的工具名一律拒绝。
每个工具带风险元数据（action / money / reversible / max_amount），
权限策略引擎直接读这些元数据，不靠模型自述。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    method: str
    path: str
    args: dict[str, type] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    defaults: dict = field(default_factory=dict)
    action: str | None = None          # 对应 policy.json 里的 base 风险分键
    money: bool = False                # True 表示会真实划转资金，必须过权限闸门
    gated: bool = False                # 不划钱但不可逆 / 需审批的动作，同样要过闸门
    reversible: bool = True
    idem: bool = False
    local: tuple[str, ...] = ()        # 只在本地消费、不发往银行侧的参数
    desc: str = ""

    @property
    def is_gated(self) -> bool:
        return self.money or self.gated


TOOLS: dict[str, ToolSpec] = {}


def _reg(spec: ToolSpec) -> None:
    TOOLS[spec.name] = spec


# ---- 只读 ----
_reg(ToolSpec("account.list", "GET", "/v1/accounts", desc="查询账户列表与余额"))
_reg(ToolSpec("account.balance", "GET", "/v1/accounts/{account_id}/balance",
              {"account_id": int, "last4": str}, ("account_id",), desc="查询单账户余额"))
_reg(ToolSpec("payee.search", "GET", "/v1/payees/search", {"q": str}, ("q",), desc="按姓名/关系检索收款人"))
_reg(ToolSpec("risk.stats", "GET", "/v1/risk/stats", desc="读取风控特征（频率/白名单）"))
# account_id 不再给默认值 1：不传时由核心系统按调用方身份取默认账户，
# 传了则核心系统校验归属。写死 1 会让第二个客户读到第一个客户的账。
_reg(ToolSpec("bill.summary", "GET", "/v1/bills/summary",
              {"from": str, "to": str, "account_id": int}, ("from", "to"), {},
              desc="账单分类汇总"))
_reg(ToolSpec("bill.anomalies", "GET", "/v1/bills/anomalies",
              {"from": str, "to": str, "account_id": int}, ("from", "to"), {},
              desc="异常交易识别"))
_reg(ToolSpec("wealth.products", "GET", "/v1/wealth/products",
              {"risk_max": str, "term_max": int, "keyword": str}, (), {"risk_max": "R3", "term_max": 9999},
              local=("keyword",), desc="理财货架检索"))
_reg(ToolSpec("wealth.holdings", "GET", "/v1/wealth/holdings",
              {"keyword": str}, (), {}, local=("keyword",), desc="查询持仓"))
_reg(ToolSpec("card.list", "GET", "/v1/cards",
              {"last4": str}, (), {}, local=("last4",), desc="查询名下卡片"))
_reg(ToolSpec("sub.list", "GET", "/v1/subscriptions",
              {"keyword": str}, (), {}, local=("keyword",), desc="查询订阅代扣"))
_reg(ToolSpec("gift.recommend", "GET", "/v1/partner/gift/recommend", {"budget": str}, ("budget",),
              desc="礼物推荐（三方）"))

# ---- 资金类（money=True，必须过权限闸门）----
_reg(ToolSpec("transfer.create", "POST", "/v1/transfers",
              {"account_id": int, "payee_id": int, "amount": str, "memo": str},
              ("account_id", "payee_id", "amount"), {},
              action="transfer.create", money=True, reversible=False, idem=True, desc="即时转账"))
_reg(ToolSpec("transfer.schedule", "POST", "/v1/transfers/schedule",
              {"account_id": int, "payee_id": int, "amount": str, "memo": str,
               "first_run_at": str, "recurrence": str},
              ("account_id", "payee_id", "amount", "first_run_at"), {},
              action="transfer.schedule", money=True, reversible=True, idem=True, desc="定时转账"))
_reg(ToolSpec("wealth.order", "POST", "/v1/wealth/orders",
              {"product_id": str, "amount": str}, ("product_id", "amount"), {},
              action="wealth.order", money=True, reversible=True, idem=True, desc="理财申购"))
_reg(ToolSpec("wealth.redeem", "POST", "/v1/wealth/redeem",
              {"holding_id": int, "amount": str}, ("holding_id",), {},
              action="wealth.redeem", money=True, reversible=False, idem=True, desc="理财赎回"))
_reg(ToolSpec("card.limit", "PATCH", "/v1/cards/{card_id}/limit",
              {"card_id": int, "daily_limit": str}, ("card_id", "daily_limit"), {},
              action="card.limit", money=False, gated=True, reversible=True, desc="调整交易限额"))
_reg(ToolSpec("card.freeze", "POST", "/v1/cards/{card_id}/freeze",
              {"card_id": int, "freeze": bool}, ("card_id",), {},
              action="card.freeze", money=False, gated=True, reversible=True, desc="冻结/解冻卡片"))
_reg(ToolSpec("card.loss", "POST", "/v1/cards/{card_id}/loss",
              {"card_id": int}, ("card_id",), {},
              action="card.loss", money=False, gated=True, reversible=False, desc="挂失（不可逆）"))
_reg(ToolSpec("card.apply", "POST", "/v1/cards/apply",
              {"card_type": str}, (), {}, action="card.apply", gated=True, reversible=True, desc="申请新卡"))
_reg(ToolSpec("sub.cancel", "POST", "/v1/subscriptions/{sub_id}/cancel",
              {"sub_id": int}, ("sub_id",), {},
              action="sub.cancel", money=False, gated=True, reversible=False, desc="取消订阅"))
_reg(ToolSpec("gift.order", "POST", "/v1/partner/gift/order",
              {"gift_id": str, "deliver_at": str}, ("gift_id",), {},
              action="gift.order", money=True, reversible=True, idem=True, desc="礼物下单（三方）"))

# ---- 非资金但改变状态 ----
_reg(ToolSpec("transfer.collect", "POST", "/v1/transfers/collect",
              {"total": str, "people": int, "memo": str}, ("total", "people"), {},
              action="transfer.collect", money=False, idem=True, desc="AA 收款"))
_reg(ToolSpec("hold.create", "POST", "/v1/holds",
              {"amount": str, "reason": str, "expire_at": str}, ("amount",), {},
              # 锁资金不转钱，但它占用可用余额、是真实的状态变更。
              # gated=False 时整条跨场景链路会被判成"纯只读"，在 preview 阶段直接执行 ——
              # 用户还没确认礼物，专项资金已经被锁了，而且全程没走 policy.decide。
              action="hold.create", gated=True, reversible=True, idem=True, desc="锁定专项资金"))
_reg(ToolSpec("hold.release", "POST", "/v1/holds/{hold_id}/release",
              {"hold_id": int}, ("hold_id",), {},
              action="hold.create", gated=True, reversible=True, idem=True, desc="释放锁定资金"))

MONEY_ACTIONS = {n for n, s in TOOLS.items() if s.money}


class RegistryInvariantError(RuntimeError):
    """注册表与策略配置对不上。这种错必须在启动期炸，不能等第一笔交易才发现。"""


def assert_invariants() -> None:
    """启动期自检：注册表是资金链路的唯一入口，它的完整性不该靠"记得改"。

    检查项都对应过真实错配：
    - 资金写操作必须可去重（`idem=True`），否则一次重试就是一笔重复扣款；
    - 受控动作必须有 `action`，否则 `Plan.primary_action()` 返回 None，策略只能按未知动作兜底；
    - 每个 `action` 都要在 policy.json 里有基础分，否则 `score()` 会直接抛（他们改成 fail-fast 了）；
    - `required` 必须是 `args` 子集，路径占位符必须取得到值（否则拼出 404 的 URL）。
    """
    from backend.security import policy

    weights = policy.config()["weights"]
    problems: list[str] = []
    for s in TOOLS.values():
        if s.money and not s.idem:
            problems.append(f"{s.name}: 资金类工具必须 idem=True（重试不能变成重复扣款）")
        if s.is_gated and not s.action:
            problems.append(f"{s.name}: 受控工具必须声明 action")
        if s.action and s.action not in weights:
            problems.append(f"{s.name}: action={s.action} 在 policy.json 的 weights 里没有基础分")
        for k in s.required:
            if k not in s.args:
                problems.append(f"{s.name}: required 里的 {k} 不在 args 声明中")
        for part in s.path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                ph = part[1:-1]
                if ph not in s.args:
                    problems.append(f"{s.name}: 路径占位符 {{{ph}}} 没有对应的参数声明")
    if problems:
        raise RegistryInvariantError("工具注册表自检失败：\n  - " + "\n  - ".join(problems))


def get(name: str) -> ToolSpec:
    if name not in TOOLS:
        raise KeyError(f"工具未注册：{name}")
    return TOOLS[name]


def catalog() -> list[dict]:
    """给规划器 / 调试台看的工具清单。"""
    return [{"name": s.name, "desc": s.desc, "args": {k: v.__name__ for k, v in s.args.items()},
             "money": s.money, "gated": s.is_gated, "reversible": s.reversible,
             "idem": s.idem, "action": s.action} for s in TOOLS.values()]
