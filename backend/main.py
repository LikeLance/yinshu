"""银枢 YinShu 后端入口。

一次请求的十步旅程：
1 输入隔离 → 2 意图理解 → 3 槽位 → 4 规划 → 5 预览（只读）→
6 权限决策 → 7（确认后）受控执行 → 8 幻觉校验 → 9 审计上链 → 10 返回卡片
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core import config, metrics
from backend.core.errors import Ambiguous, YinshuError
from backend.core.logging import get
from backend.core.middleware import ApiKeyGate, BodyLimit, RateLimit, RequestContext
from backend.nlu import intent as nlu
from backend.nlu.slots import parse_amount, parse_people, parse_payee, parse_when
from backend.planner import executor, planner
from backend.planner.contracts import Plan, Step
from backend.presenter import build as build_presentation, plan_card
from backend.security import audit, guard, injection, policy
from backend.session import fsm
from backend.tools import gateway, registry

log = get("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动：打印关键配置 + 起一个后台清扫任务；关闭：取消任务。"""
    log.info("startup", env=config.ENV, version=config.APP_VERSION,
             session_backend=config.SESSION_BACKEND, rate_limit_rpm=config.RATE_LIMIT_RPM,
             workers=config.WORKERS, auth="api-key" if config.API_KEYS else "off")
    if config.IS_PROD and not config.API_KEYS:
        log.warning("auth_disabled_in_prod",
                    hint="生产环境建议设置 YINSHU_API_KEYS，或由网关统一鉴权")

    async def _sweeper():
        while True:
            await asyncio.sleep(300)
            try:
                n = fsm.purge_expired()
                if n:
                    log.info("session_purged", count=n)
            except Exception:                                     # noqa: BLE001
                log.exception("session_purge_failed")

    task = asyncio.create_task(_sweeper())
    try:
        yield
    finally:
        task.cancel()
        gateway.close()          # 优雅关掉下游连接池
        log.info("shutdown")


app = FastAPI(
    title="银枢 YinShu · 可信银行智能体",
    version=config.APP_VERSION,
    description="安全策略下沉到执行层的银行 AI 智能体。六大赛道场景全部可演示，"
                "权限分级由代码级策略引擎决定，不依赖提示词。",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# 中间件：先打请求上下文，再限流，再校验体积（顺序见 middleware.py 注释）
app.add_middleware(BodyLimit)
app.add_middleware(RateLimit)
app.add_middleware(ApiKeyGate)
app.add_middleware(RequestContext)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-Id"],
    max_age=600,
)

api = APIRouter()

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
CUSTOMER_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}


class ChatReq(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=config.MAX_TEXT_LEN)
    device_trusted: bool = True


def _jsonable(o: Any) -> Any:
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


def _stats() -> dict:
    try:
        return gateway.call("risk.stats", {})
    except YinshuError:
        return {}


# ---------------- 决策上下文 ----------------

def _context(plan: Plan, slots: dict, out: executor.Outcome, stats: dict, inj: bool,
             s: fsm.Session) -> policy.Context:
    action = plan.primary_action() or "transfer.create"
    amt = Decimal("0")
    for k in ("amount", "budget", "daily_limit"):
        if slots.get(k) not in (None, "", []):
            try:
                amt = Decimal(str(slots[k]))
                break
            except Exception:
                pass

    payee = ((out.results.get("s1") or {}).get("payee") or {})
    pid = payee.get("id")
    known = stats.get("known_payees") or []
    recent = stats.get("recent_payees") or []

    unsuitable = False
    if action == "wealth.order":
        picked = (out.results.get("s1") or {}).get("picked") or {}
        level = stats.get("risk_level", "C3")
        if picked.get("risk"):
            unsuitable = RISK_ORDER[picked["risk"]] > CUSTOMER_ORDER[level]

    return policy.Context(
        action=action, amount=amt,
        payee_blacklisted=bool(payee.get("blacklisted")),
        payee_whitelisted=pid in known if pid else True,
        payee_new=(pid not in recent) if pid else False,
        payee_relation=payee.get("relation") or "",
        cross_border=("香港" in str(payee.get("city") or "")) or ("HSBC" in str(payee.get("bank") or "")),
        device_trusted=s.device_trusted,
        hour=datetime.now().hour,
        velocity_1h=stats.get("velocity_1h", 0),
        daily_count=stats.get("daily_count", 0),
        segment=stats.get("segment", "普通"),
        wealth_unsuitable=unsuitable,
        injection_flagged=inj,
    )


def _confirm_rows(plan: Plan, slots: dict, out: executor.Outcome) -> list[list[str]]:
    rows: list[list[str]] = []
    payee = ((out.results.get("s1") or {}).get("payee") or {})
    if payee:
        rows.append(["收款人", f"{payee.get('name')}（{payee.get('relation')}，尾号 {payee.get('account_last4')}）"])
    if slots.get("amount") is not None:
        rows.append(["金额", f"{Decimal(str(slots['amount'])):.2f} 元"])
    if slots.get("budget") is not None:
        rows.append(["预算", f"{Decimal(str(slots['budget'])):.2f} 元"])
    if slots.get("gift_name"):
        rows.append(["礼物", slots["gift_name"]])
        if slots.get("gift_amount"):
            rows.append(["金额", f"{Decimal(str(slots['gift_amount'])):.2f} 元"])
    w = slots.get("when") or {}
    if w.get("label"):
        rows.append(["时间", w["label"]])
    if slots.get("memo"):
        rows.append(["备注", slots["memo"]])
    picked = (out.results.get("s1") or {}).get("picked")
    if isinstance(picked, dict):
        if picked.get("name"):
            rows.append(["产品" if "risk" in picked else "标的", f"{picked['name']}"])
        if picked.get("risk"):
            rows.append(["风险等级", f"{picked['risk']} / 业绩基准 {picked.get('annual_rate')}%"])
        if picked.get("merchant"):
            rows.append(["订阅", f"{picked['merchant']}（{picked['amount']} 元/{picked['cycle']}）"])
        if picked.get("last4"):
            rows.append(["卡片", f"尾号 {picked['last4']}"])
        if picked.get("amount") and "cycle" not in picked:
            rows.append(["持仓金额", f"{picked['amount']} 元"])
    if not rows:
        rows.append(["动作", plan.steps[-1].label or plan.steps[-1].tool])
    return rows


def _notice(tone: str, title: str, lines: list[str], **extra) -> dict:
    return {"type": "notice", "tone": tone, "title": title, "lines": lines, **extra}


# ---------------- 主处理 ----------------

def _handle(s: fsm.Session, text: str, intent: str, slots: dict, trace: str,
            inj: bool, forced_payee: int | None = None) -> dict:
    plan = planner.build(intent, slots)
    if plan.missing:
        q = {"payee": "转给谁？说姓名或关系就行（比如「妈」「李慧」）。",
             "amount": "转多少？",
             "when": "什么时候转？",
             "people": "一共几个人 AA？",
             "daily_limit": "想调到多少的日限额？",
             "product": "要买/赎回哪支产品？说个名字关键词就行。",
             "merchant": "要取消哪个订阅？说商户名关键词就行。",
             "period": "想看哪段时间的账？"}
        # 卡片里放「可以怎么答」，避免和回复正文重复一遍同一句话
        hint = {"payee": "可以说「妈」「我妈」「李慧」",
                "amount": "可以说「500」「八百」「一千二」",
                "when": "可以说「下周三」「每月1号」「下午3点」",
                "people": "直接说人数，比如「4个人」",
                "daily_limit": "比如「调到3万」",
                "product": "说产品名里的两三个字",
                "merchant": "说商户名里的两三个字",
                "period": "比如「这个月」「上个月」「最近三个月」"}
        ask = " ".join(q.get(m, m) for m in plan.missing)
        fsm.ask(s, "CLARIFY", {"intent": intent, "slots": slots})
        return {"reply": ask,
                "card": _notice("info", "还差一点信息",
                                [hint.get(m, q.get(m, m)) for m in plan.missing]),
                "debug": {"intent": intent, "missing": plan.missing}}

    # ---- 预览：只跑只读步骤 ----
    out = executor.Outcome()
    try:
        executor.run(plan, "preview", trace, out, ignore_ambiguous=bool(forced_payee))
    except Ambiguous as e:
        fsm.ask(s, "CHOOSE", {"intent": intent, "slots": slots, "kind": e.kind,
                              "options": e.options, "forced": forced_payee})
        return _choose_response(e.kind, e.options, intent)
    except YinshuError as e:
        # 业务异常不该变成 HTTP 500：对话里说清楚，用户还能接着往下办
        fsm.reset(s)
        return {"reply": e.user_msg,
                "card": _notice("info", "这一步没走通", [e.user_msg]),
                "debug": {"intent": intent, "business_error": e.code}}

    if forced_payee and "s1" in out.results:
        out.results["s1"]["chosen"] = forced_payee
        out.results["s1"]["ambiguous"] = False
        out.results["s1"]["payee"] = next(
            (c for c in out.results["s1"].get("candidates", []) if c["id"] == forced_payee),
            out.results["s1"].get("payee"))

    # 不可逆/限额类卡片操作：名下有多张卡又没说哪张时，必须先问，不能默认挑一张
    if intent in ("card.loss", "card.freeze", "card.limit"):
        cards = (out.results.get("s1") or {}).get("cards") or []
        if len(cards) > 1 and not str(slots.get("card_last4") or "").strip():
            fsm.ask(s, "CHOOSE", {"intent": intent, "slots": slots, "kind": "card",
                                  "options": cards})
            return _choose_response("card", cards, intent)

    for res in out.results.values():
        if isinstance(res, dict) and res.get("not_found"):
            if intent == "card.list":
                # 查卡属于只读场景：查不到也照样把现有卡片摆出来，别让用户空手而归
                res.pop("not_found", None)
                continue
            fsm.reset(s)
            if intent == "sub.cancel":
                return {"reply": "您名下已经没有这项在生效的自动续费了——可能之前就取消过。",
                        "card": _notice("info", "没有可取消的这一项",
                                        ["说「有哪些自动续费」可以看当前在扣的清单"]),
                        "debug": {"intent": intent, "not_found": True}}
            return {"reply": "没找到对得上的那一条，说个更准确的关键词？",
                    "card": _notice("info", "没找到匹配项", ["比如直接说商户名或产品名里的两三个字"]),
                    "debug": {"intent": intent, "not_found": True}}

    gated = [st for st in plan.steps if st.is_gated]

    # ---- 纯只读场景：直接跑完 ----
    if not gated:
        try:
            executor.run(plan, "commit", trace, out)
        except YinshuError as e:
            fsm.reset(s)
            return {"reply": e.user_msg, "card": _notice("warn", "查询未完成", [e.user_msg]),
                    "debug": {"intent": intent, "error": e.code}}
        text_out, card, extra = build_presentation(intent, plan, out, None, slots)
        if card and card.get("type") == "choose":
            fsm.ask(s, "CHOOSE", {"intent": intent, "slots": slots, "kind": card.get("kind"),
                                  "options": (out.results.get("s2") or {}).get("gifts") or []})
        all_facts = executor.facts(slots, out.results, extra)
        bad = guard.check(text_out, all_facts)
        audit.append(trace, "read_only", {"intent": intent}, "ok")
        return {"reply": text_out, "card": card,
                "debug": {"intent": intent, "guard_violations": bad,
                          "plan": [st.model_dump() for st in plan.steps]}}

    # ---- 权限决策 ----
    stats = _stats()
    ctx = _context(plan, slots, out, stats, inj, s)
    d = policy.decide(ctx)
    audit.append(trace, "policy_decision", {"action": ctx.action, "amount": str(ctx.amount),
                                            "risk": d.risk, "tier": d.tier,
                                            "reasons": d.reasons}, d.tier.lower())

    if d.tier in ("DENY", "HANDOFF"):
        fsm.reset(s)
        title = "已拒绝执行" if d.tier == "DENY" else "需人工复核"
        ticket = f"TK{datetime.now().strftime('%m%d%H%M%S')}"
        return {"reply": ("这笔我不能替您办。" if d.tier == "DENY" else
                          f"这笔需要人工介入，工单号 {ticket}，客服会在 30 分钟内联系您。"),
                "card": _notice("danger" if d.tier == "DENY" else "warn", title,
                                d.reasons + ([f"工单号 {ticket}"] if d.tier == "HANDOFF" else [])),
                "debug": {"intent": intent, "decision": d.to_dict()}}

    if d.tier == "CONFIRM":
        fsm.await_confirm(s, {"plan": plan, "out": out, "decision": d, "slots": slots,
                              "intent": intent, "double": d.require_double_confirm, "confirm_n": 0})
        card = {"type": "confirm", "title": "请确认这笔操作",
                "rows": _confirm_rows(plan, slots, out),
                "reasons": d.reasons, "risk": d.risk, "tier": d.tier,
                "need_double": d.require_double_confirm,
                "note": "点确认后才会真正执行；说「取消」随时终止"}
        return {"reply": f"这笔需要您确认一下（风险分 {d.risk}）。{('这是不可逆操作，确认后无法撤销。' if d.require_double_confirm else '')}",
                "card": card,
                "debug": {"intent": intent, "decision": d.to_dict(),
                          "plan": [st.model_dump() for st in plan.steps]}}

    # ---- AUTO：直接执行 ----
    try:
        executor.run(plan, "commit", trace, out)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code, "compensated": out.compensated}, "error")
        fsm.reset(s)
        return {"reply": e.user_msg, "card": _notice("danger", "执行未完成",
                                                     [e.user_msg, *(["已自动回滚"] if out.compensated else [])]),
                "debug": {"intent": intent, "error": e.code, "compensated": out.compensated}}
    text_out, card, extra = build_presentation(intent, plan, out, d, slots)
    all_facts = executor.facts(slots, out.results, extra)
    bad = guard.check(text_out, all_facts)
    if bad:
        text_out = "操作已完成，详细结果见下方卡片。"
        audit.append(trace, "guard_rewrite", {"violations": bad}, "ok")
    fsm.reset(s)
    return {"reply": text_out, "card": card,
            "debug": {"intent": intent, "decision": d.to_dict(), "guard_violations": bad,
                      "plan": [st.model_dump() for st in plan.steps]}}


def _choose_response(kind: str, options: list[dict], intent: str) -> dict:
    if kind == "payee":
        opts = [{"id": str(o["id"]), "label": f"{o['name']}（{o.get('relation')}）",
                 "sub": f"{o.get('bank')} 尾号 {o.get('account_last4')}"} for o in options]
        title, reply = "找到多位可能的收款人", "通讯录里有几位对得上，选一位我再往下办。"
    elif kind == "gift":
        opts = [{"id": o["gift_id"], "label": o["name"], "sub": f"{o['amount']} 元"} for o in options]
        title, reply = "选一个方案", "选一个我来下单。"
    elif kind == "card":
        st_map = {"active": "正常", "frozen": "已冻结", "lost": "已挂失"}
        opts = [{"id": str(o["last4"]), "label": f"尾号 {o['last4']}（{o['brand']}）",
                 "sub": f"{st_map.get(o['status'], o['status'])} · 日限额 {o['daily_limit']} 元"}
                for o in options]
        title = "要操作哪张卡"
        reply = f"名下有 {len(options)} 张卡，先确认是哪一张——这一步不可逆，我不替你猜。"
    else:
        opts = [{"id": str(o.get("id", o.get("gift_id", ""))),
                 "label": o.get("name") or o.get("merchant") or str(o.get("id")),
                 "sub": str(o.get("amount", ""))} for o in options]
        title, reply = "找到多个匹配项", "选一个继续。"
    return {"reply": reply, "card": {"type": "choose", "title": title, "kind": kind, "options": opts},
            "debug": {"intent": intent, "ambiguous": kind}}


def _pick_option(s: fsm.Session, text: str) -> dict | None:
    opts = s.stash.get("options") or []
    t = text.strip()
    idx = None
    for i, w in enumerate(["一", "二", "三", "四", "五"], start=1):
        if f"第{w}" in t or t == w:
            idx = i - 1
            break
    if idx is None:
        import re
        m = re.search(r"第?\s*([1-9])\s*(?:个|支|款|号)?", t)
        if m:
            idx = int(m.group(1)) - 1
    if idx is not None and 0 <= idx < len(opts):
        return opts[idx]
    for o in opts:
        label = o.get("name") or o.get("merchant") or ""
        if label and label in t:
            return o
        if o.get("relation") and o["relation"] in t:
            return o
        if o.get("last4") and str(o["last4"]) in t:      # 用户直接报尾号
            return o
    return opts[0] if len(opts) == 1 else None


def _merge_clarify(s: fsm.Session, text: str) -> tuple[str, dict]:
    st = s.stash
    slots = dict(st.get("slots") or {})
    amt = parse_amount(text)
    if amt is not None:
        slots["amount"] = amt
    p = parse_payee(text)
    if not p:
        cand = text.strip().strip("给转付打 ")
        if 1 <= len(cand) <= 4 and all("\u4e00" <= c <= "\u9fa5" for c in cand):
            p = cand
    if p:
        slots["payee_raw"] = p
    w = parse_when(text)
    if w.get("explicit"):
        slots["when"] = w
    pp = parse_people(text)
    if pp:
        slots["people"] = pp
    if st.get("intent") == "wealth.subscribe" and not slots.get("product_keyword") and len(text) <= 12:
        slots["product_keyword"] = text.strip()
    if st.get("intent") == "sub.cancel" and not slots.get("merchant_keyword"):
        slots["merchant_keyword"] = text.strip()
    if st.get("intent") == "wealth.redeem" and not slots.get("product_keyword"):
        slots["product_keyword"] = text.strip()
    return st.get("intent") or "unknown", slots


@api.post("/chat")
def chat(req: ChatReq):
    t0 = time.time()
    trace = uuid.uuid4().hex[:16]
    s = fsm.get(req.session_id)
    # 设备信任是"这次请求携带的信号"，只用于本次决策打分；不落盘，避免旧值污染后续判断
    s.device_trusted = req.device_trusted
    text = (req.text or "").strip()

    scan = injection.scan(text)
    audit.append(trace, "user_input", {"text": text, "session": s.sid,
                                       "injection": scan.to_dict()},
                 "blocked" if scan.level == "high" else "ok")

    out: dict
    # ---------- 确认态 ----------
    if s.state == "AWAIT_CONFIRM":
        if s.expire_pending():
            return JSONResponse(_resp(s, trace, "确认已超时作废，请重新发起。", None,
                                      {"expired": True}, t0))
        if nlu.is_cancel(text):
            fsm.reset(s)
            return JSONResponse(_resp(s, trace, "好的，已取消，这笔不会执行。",
                                      _notice("info", "已取消", ["未发生任何资金变动"]),
                                      {"cancelled": True}, t0))
        if nlu.is_confirm(text):
            if s.stash.get("double") and s.stash.get("confirm_n", 0) < 1:
                s.stash["confirm_n"] = 1
                fsm.save(s)          # 会话已落盘，改了计数必须写回，否则第二次确认会再问一遍
                return JSONResponse(_resp(
                    s, trace, "这是不可逆操作。确认无误请再回复一次「确认」。",
                    _notice("warn", "需要二次确认", ["挂失后原卡立即失效，不可撤销"]),
                    {"double_confirm": 1}, t0))
            return JSONResponse(_execute_pending(s, trace, t0))
        fsm.reset(s)   # 其他输入：先撤销待确认事务，再按新指令处理

    # ---------- 选择态 ----------
    if s.state == "CHOOSE":
        opt = _pick_option(s, text)
        if opt is None:
            return JSONResponse(_resp(s, trace, "没听清是哪一个，回个序号（第1个/第2个）就行。",
                                      _choose_response(s.stash.get("kind", "item"),
                                                       s.stash.get("options") or [],
                                                       s.stash.get("intent", "")).get("card"),
                                      {}, t0))
        kind = s.stash.get("kind")
        intent, slots = s.stash.get("intent"), s.stash.get("slots") or {}
        fsm.reset(s)
        if kind == "gift":
            plan = Plan(intent="gift.order", slots={**slots, "gift_id": opt["gift_id"],
                                                    "gift_name": opt.get("name"),
                                                    "gift_amount": opt.get("amount")},
                        steps=[Step(id="s1", tool="gift.order",
                                    args={"gift_id": opt["gift_id"],
                                          "deliver_at": (slots.get("when") or {}).get("date") or ""},
                                    label=f"下单「{opt.get('name')}」")])
            res = _run_plan(s, plan, "gift.order", plan.slots, trace, scan.level != "clean")
            return JSONResponse({**_resp(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})
        forced = int(opt["id"]) if kind == "payee" else None
        if kind == "card":
            slots = {**slots, "card_last4": str(opt.get("last4") or opt.get("id"))}
            res = _handle(s, text, intent, slots, trace, scan.level != "clean")
            return JSONResponse({**_resp(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})
        res = _handle(s, text, intent, slots, trace, scan.level != "clean", forced)
        return JSONResponse({**_resp(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})

    # ---------- 澄清态 ----------
    if s.state == "CLARIFY":
        intent, slots = _merge_clarify(s, text)
        fsm.reset(s)
        res = _handle(s, text, intent, slots, trace, scan.level != "clean")
        return JSONResponse({**_resp(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})

    # ---------- 注入拦截 ----------
    if scan.level == "high":
        audit.append(trace, "injection_block", scan.to_dict(), "blocked")
        return JSONResponse(_resp(
            s, trace, "这段请求超出了我能代办的范围，换个说法试试？",
            _notice("danger", "请求已被拦截", scan.hits[:3]),
            # plan 恒为空数组：拦截发生在规划之前，客户端据此可判断"一步都没执行"
            {"intent": None, "injection": scan.to_dict(), "plan": [], "blocked": True}, t0))

    # ---------- 正常意图 ----------
    p = nlu.parse(text)
    if p["intent"] == "unknown":
        fsm.reset(s)
        if nlu.is_confirm(text):
            return JSONResponse(_resp(
                s, trace, "现在没有待确认的操作——上一笔已经办完了。",
                _notice("info", "没有待确认的事", ["可以直接说下一件事"]),
                {"no_pending": True}, t0))
        return JSONResponse(_resp(
            s, trace, "没太听明白。我能办：转账、账单、理财、卡片、订阅，还有跨场景的生日安排。",
            {"type": "notice", "tone": "info", "title": "可以试试这些",
             "lines": ["给我妈转500，备注生活费", "这个月钱都花哪了",
                       "有哪些自动续费", "下周三我妈生日，帮我准备一下"]},
            {"intent": "unknown", "confidence": p["confidence"]}, t0))

    res = _handle(s, text, p["intent"], p["slots"], trace, scan.level != "clean")
    dbg = {"intent": p["intent"], "intent_label": nlu.INTENT_LABEL.get(p["intent"]),
           "confidence": p["confidence"], "slots": p["slots"], **res.get("debug", {})}
    return JSONResponse({**_resp(s, trace, res["reply"], res["card"], dbg, t0)})


def _run_plan(s: fsm.Session, plan: Plan, intent: str, slots: dict, trace: str, inj: bool) -> dict:
    """给"非 NLU 构造的 plan"（如礼物下单）用的执行入口。"""
    out = executor.Outcome()
    executor.run(plan, "preview", trace, out)
    stats = _stats()
    ctx = _context(plan, slots, out, stats, inj, s)
    d = policy.decide(ctx)
    audit.append(trace, "policy_decision", {"action": ctx.action, "risk": d.risk, "tier": d.tier},
                 d.tier.lower())
    if d.tier in ("DENY", "HANDOFF"):
        fsm.reset(s)
        return {"reply": "这笔我不能替您办。" if d.tier == "DENY" else "这笔需要人工复核。",
                "card": _notice("danger" if d.tier == "DENY" else "warn",
                                "已拒绝执行" if d.tier == "DENY" else "需人工复核", d.reasons),
                "debug": {"decision": d.to_dict()}}
    if d.tier == "CONFIRM":
        fsm.await_confirm(s, {"plan": plan, "out": out, "decision": d, "slots": slots,
                              "intent": intent, "double": d.require_double_confirm, "confirm_n": 0})
        return {"reply": f"这笔需要您确认（风险分 {d.risk}）。",
                "card": {"type": "confirm", "title": "请确认这笔操作",
                         "rows": _confirm_rows(plan, slots, out), "reasons": d.reasons,
                         "risk": d.risk, "tier": d.tier, "need_double": False},
                "debug": {"decision": d.to_dict()}}
    try:
        executor.run(plan, "commit", trace, out)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code}, "error")
        fsm.reset(s)
        msg = e.user_msg or "执行失败，请稍后再试。"
        return {"reply": msg, "card": _notice("danger", "执行未完成", [msg]),
                "debug": {"decision": d.to_dict(), "error": e.code,
                          "compensated": out.compensated}}
    text_out, card, extra = build_presentation(intent, plan, out, d, slots)
    bad = guard.check(text_out, executor.facts(slots, out.results, extra))
    fsm.reset(s)
    return {"reply": text_out, "card": card, "debug": {"decision": d.to_dict(), "guard_violations": bad}}


def _execute_pending(s: fsm.Session, trace: str, t0: float) -> dict:
    stash = s.stash
    plan: Plan = stash["plan"]
    out: executor.Outcome = stash["out"]
    d: policy.Decision = stash["decision"]
    intent, slots = stash["intent"], stash["slots"]
    try:
        executor.run(plan, "commit", trace, out)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code}, "error")
        fsm.reset(s)
        msg = e.user_msg or "执行失败，请稍后再试。"
        return _resp(s, trace, msg, _notice("danger", "执行未完成", [msg]),
                     {"error": e.code, "compensated": out.compensated}, t0)

    text_out, card, extra = build_presentation(intent, plan, out, d, slots)
    bad = guard.check(text_out, executor.facts(slots, out.results, extra))
    if bad:
        text_out = "操作已完成，详细结果见下方卡片。"
        audit.append(trace, "guard_rewrite", {"violations": bad}, "ok")
    audit.append(trace, "execute_ok", {"intent": intent, "compensated": out.compensated}, "ok")
    fsm.reset(s)
    return _resp(s, trace, text_out, card,
                 {"decision": d.to_dict(), "guard_violations": bad,
                  "compensated": out.compensated}, t0)


def _resp(s: fsm.Session, trace: str, reply: str, card: dict | None, debug: dict, t0: float) -> dict:
    chain = audit.append(trace, "response", {"reply": reply[:80]}, "ok")
    # 补齐三件套，保证任何返回路径的 debug 结构一致——集成方不用按分支猜字段
    d = {"guard_violations": [], "plan": [], "state": s.state, **debug}
    return _jsonable({"session_id": s.sid, "trace_id": trace, "state": s.state,
                      "reply": reply, "card": card,
                      "debug": {**d, "audit": chain,
                                "latency_ms": int((time.time() - t0) * 1000)}})


# ---------------- 运维 / 调试接口 ----------------

@api.get("/health")
def health():
    bank = {}
    try:
        bank = gateway.call("account.list", {}) and {"ok": True}
    except Exception:
        bank = {"ok": False}
    return {"ok": True, "bank": bank, "audit": audit.stats(),
            "llm": "rule-engine (deterministic)", "policy": policy.config()["tiers"],
            "version": config.APP_VERSION, "env": config.ENV}


@api.get("/policy")
def get_policy():
    return {"config": policy.config(), "matrix": policy.matrix()}


@api.get("/tools")
def get_tools():
    return {"tools": registry.catalog(), "count": len(registry.TOOLS)}


@api.get("/audit/verify")
def audit_verify():
    return audit.verify()


@api.get("/audit/{trace_id}")
def audit_trace(trace_id: str):
    return {"trace_id": trace_id, "logs": audit.history(trace_id)}


@api.get("/plan/{text}")
def preview_plan(text: str):
    """调试台用：只出计划，不执行。"""
    p = nlu.parse(text)
    plan = planner.build(p["intent"], p["slots"])
    return {"intent": p["intent"], "slots": _jsonable(p["slots"]),
            "steps": [st.model_dump() for st in plan.steps], "missing": plan.missing}


# ---------------- 探针（K8s / Compose 都用得上）----------------

@app.get("/healthz", include_in_schema=False)
def healthz():
    """存活探针：进程活着就 200，不查依赖。"""
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
def readyz():
    """就绪探针：核心系统连得上才算就绪，避免把请求打进来才发现下游挂了。"""
    try:
        gateway.call("account.list", {})
    except Exception as e:                                       # noqa: BLE001
        return JSONResponse({"ok": False, "dependency": "mock_bank", "detail": str(e)[:120]},
                            status_code=503)
    return {"ok": True, "dependency": "mock_bank"}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    if not config.METRICS_ENABLED:
        return JSONResponse({"ok": False, "error": {"code": "DISABLED", "message": "指标未启用"}},
                            status_code=404)
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------- 统一错误处理 ----------------

def _err(code: str, message: str, status: int, request: Request, extra: dict | None = None):
    rid = getattr(request.state, "request_id", "")
    metrics.inc("yinshu_errors_total", code=code)
    body = {"ok": False, "error": {"code": code, "message": message, "request_id": rid}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(body, status_code=status, headers={"x-request-id": rid})


@app.exception_handler(YinshuError)
async def _on_yinshu_error(request: Request, exc: YinshuError):
    status = 400
    if exc.code in ("POLICY_DENY", "INJECTION_BLOCKED"):
        status = 403
    elif exc.code in ("LIMIT_EXCEEDED", "INSUFFICIENT_FUNDS"):
        status = 409
    log.warning("business_error", code=exc.code, msg=str(exc)[:120],
                request_id=getattr(request.state, "request_id", ""))
    return _err(exc.code, exc.user_msg, status, request)


@app.exception_handler(RequestValidationError)
async def _on_validation(request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return _err("INVALID_REQUEST", f"参数不合法：{loc or '请求体'} {first.get('msg', '')}".strip(),
                422, request)


@app.exception_handler(StarletteHTTPException)
async def _on_http(request: Request, exc: StarletteHTTPException):
    return _err("NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                str(exc.detail), exc.status_code, request)


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    # 生产环境绝不把堆栈回给调用方，只留 request_id 供排查
    log.exception("unhandled", request_id=getattr(request.state, "request_id", ""))
    return _err("INTERNAL_ERROR", "服务内部错误，请稍后重试或联系支持", 500, request)


# ---------------- 装配 ----------------

app.include_router(api, prefix="/api")
app.include_router(api, prefix="/api/v1")     # 版本化入口，行为与 /api 完全一致


@app.get("/", include_in_schema=False)
def index():
    f = FRONTEND / "index.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"msg": "前端未找到", "path": str(f)})

