"""银枢 YinShu 后端入口。

一次请求的十步旅程：
1 输入隔离 → 2 意图理解 → 3 槽位 → 4 规划 → 5 预览（只读）→
6 权限决策 → 7（确认后）受控执行 → 8 幻觉校验 → 9 审计上链 → 10 返回卡片
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core import clock, config, idempotency as idem, metrics, reqctx
from backend.core.errors import Ambiguous, YinshuError
from backend.core.logging import get
from backend.core.middleware import (ApiKeyGate, BodyLimit, RateLimit,
                                     RequestContext, VersionAlias)
from backend.nlu import intent as nlu
from backend.nlu.slots import parse_amount, parse_people, parse_payee, parse_when
from backend.planner import executor, fingerprint, planner
from backend.planner.contracts import Plan, Step
from backend.presenter import build as build_presentation, plan_card
from backend.security import audit, guard, identity, injection, policy
from backend.services.choose import choose_response, merge_clarify, pick_option
from backend.services.confirm import confirm_card, execute_pending, stash_pending
from backend.services.context import (build_context, compute_drift, risk_stats,
                                      take_snapshot)
from backend.services.view import (build_response, card_text, first_of, guard_check,
                                   jsonable, notice, scan_tool_output)
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
        last_anchor = 0.0
        while True:
            await asyncio.sleep(min(60, max(1, config.ANCHOR_INTERVAL)))
            try:
                n = fsm.purge_expired()
                if n:
                    log.info("session_purged", count=n)
                m = audit.purge_pii_expired()
                if m:
                    log.info("pii_purged", count=m)
            except Exception:                                     # noqa: BLE001
                log.exception("session_purge_failed")
            # 链尾锚定：把 head 外发到 WORM/存证服务，才能发现"整表重写 + 本地重算"
            if time.time() - last_anchor >= config.ANCHOR_INTERVAL:
                try:
                    r = audit.anchor_tail()
                    if r.get("ok"):
                        log.info("audit_anchored", seq=r["seq"], ext_ref=r["ext_ref"])
                    else:
                        log.warning("audit_anchor_skipped", reason=r.get("reason"))
                except Exception:                                 # noqa: BLE001
                    log.exception("audit_anchor_failed")
                last_anchor = time.time()

    task = asyncio.create_task(_sweeper())
    try:
        yield
    finally:
        task.cancel()
        gateway.close()          # 优雅关掉下游连接池
        audit.close()
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

# 中间件执行顺序（外 → 内）。Starlette 的 add_middleware 是"后加者在外"，
# 所以这里的书写顺序是内 → 外，实际执行为：
#   CORS → RequestContext → RateLimit → ApiKeyGate → BodyLimit → 路由
# 限流必须在鉴权之外：否则未授权请求在 ApiKeyGate 就被 401 掉，
# 永远消耗不到令牌桶，401 可以被无限刷。
app.add_middleware(BodyLimit)
app.add_middleware(VersionAlias)      # 最内层：进路由前把 /api/v1/* 改写成 /api/*
app.add_middleware(ApiKeyGate)
app.add_middleware(RateLimit)
app.add_middleware(RequestContext)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-Id", "X-Idempotency-Key",
                   "X-Device-Id", "Authorization"],
    max_age=600,
)

api = APIRouter()

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class ChatReq(BaseModel):
    """对话请求体。

    `extra="forbid"`：身份只能来自 Authorization 头。请求体里出现 user_id / uid
    这类字段直接 422——"身份由调用方自述"是这个项目最危险的一个默认。

    `device_trusted` 保留是为了不破坏既有前端，但**服务端不再采信**：
    设备是否可信由 `identity.device_trusted()` 按历史行为判定。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=config.MAX_TEXT_LEN)
    device_trusted: bool = Field(default=True, deprecated=True,
                                 description="已忽略；设备信任由服务端判定")



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
                "card": notice("info", "还差一点信息",
                                [hint.get(m, q.get(m, m)) for m in plan.missing]),
                "debug": {"intent": intent, "missing": plan.missing}}

    # ---- 预览：只跑只读步骤 ----
    out = executor.Outcome()
    try:
        executor.run(plan, "preview", trace, out, ignore_ambiguous=bool(forced_payee))
    except Ambiguous as e:
        fsm.ask(s, "CHOOSE", {"intent": intent, "slots": slots, "kind": e.kind,
                              "options": e.options, "forced": forced_payee})
        return choose_response(e.kind, e.options, intent)
    except YinshuError as e:
        # 业务异常不该变成 HTTP 500：对话里说清楚，用户还能接着往下办
        fsm.reset(s)
        return {"reply": e.user_msg,
                "card": notice("info", "这一步没走通", [e.user_msg]),
                "debug": {"intent": intent, "business_error": e.code}}

    if forced_payee and "s1" in out.results:
        out.results["s1"]["chosen"] = forced_payee
        out.results["s1"]["ambiguous"] = False
        out.results["s1"]["payee"] = next(
            (c for c in out.results["s1"].get("candidates", []) if c["id"] == forced_payee),
            out.results["s1"].get("payee"))

    # L2 间接注入：工具返回的备注/商户名/产品名同样是"会进入上下文的数据"。
    # 原实现只在 injection.py 里定义了 scan_untrusted_data 却没人调用——
    # 这条防线是断的，而银行场景里"商户名"恰恰是最容易被人塞指令的地方。
    indirect = scan_tool_output(out)
    if indirect.flagged:
        inj = True
        audit.append(trace, "indirect_injection",
                     {"hits": indirect.hits, "weight": indirect.weight, "level": indirect.level},
                     "blocked" if indirect.level == "high" else "ok")

    # 不可逆/限额类卡片操作：名下有多张卡又没说哪张时，必须先问，不能默认挑一张
    if intent in ("card.loss", "card.freeze", "card.limit"):
        cards = (out.results.get("s1") or {}).get("cards") or []
        if len(cards) > 1 and not str(slots.get("card_last4") or "").strip():
            fsm.ask(s, "CHOOSE", {"intent": intent, "slots": slots, "kind": "card",
                                  "options": cards})
            return choose_response("card", cards, intent)

    for res in out.results.values():
        if isinstance(res, dict) and res.get("not_found"):
            if intent == "card.list":
                # 查卡属于只读场景：查不到也照样把现有卡片摆出来，别让用户空手而归
                res.pop("not_found", None)
                continue
            fsm.reset(s)
            if intent == "sub.cancel":
                return {"reply": "您名下已经没有这项在生效的自动续费了——可能之前就取消过。",
                        "card": notice("info", "没有可取消的这一项",
                                        ["说「有哪些自动续费」可以看当前在扣的清单"]),
                        "debug": {"intent": intent, "not_found": True}}
            return {"reply": "没找到对得上的那一条，说个更准确的关键词？",
                    "card": notice("info", "没找到匹配项", ["比如直接说商户名或产品名里的两三个字"]),
                    "debug": {"intent": intent, "not_found": True}}

    gated = [st for st in plan.steps if st.is_gated]

    # ---- 纯只读场景：直接跑完 ----
    if not gated:
        try:
            executor.run(plan, "commit", trace, out)
        except YinshuError as e:
            fsm.reset(s)
            return {"reply": e.user_msg, "card": notice("warn", "查询未完成", [e.user_msg]),
                    "debug": {"intent": intent, "error": e.code}}
        text_out, card, extra = build_presentation(intent, plan, out, None, slots)
        if card and card.get("type") == "choose":
            # 生日流程是「先锁预算 → 选礼物 → 下单」：把 hold_id 带进下一阶段，
            # 下单失败时才有东西可补偿（否则锁住的预算永远挂在 holds 表里）。
            hid = first_of(out.results, "hold_id")
            stash_slots = {**slots, **({"hold_id": hid} if hid else {})}
            fsm.ask(s, "CHOOSE", {"intent": intent, "slots": stash_slots,
                                  "kind": card.get("kind"),
                                  "options": (out.results.get("s2") or {}).get("gifts") or []})
        all_facts = executor.facts(slots, out.results, extra)
        bad = guard_check(text_out, card, all_facts)
        audit.append(trace, "read_only", {"intent": intent}, "ok")
        return {"reply": text_out, "card": card,
                "debug": {"intent": intent, "guard_violations": bad,
                          "plan": [st.model_dump() for st in plan.steps]}}

    # ---- 权限决策 ----
    stats = risk_stats()
    ctx = build_context(plan, slots, out, stats, inj, s)
    d = policy.decide(ctx)
    audit.append(trace, "policy_decision", {"action": ctx.action, "amount": str(ctx.amount),
                                            "risk": d.risk, "tier": d.tier,
                                            "reasons": d.reasons}, d.tier.lower())

    if d.tier in ("DENY", "HANDOFF"):
        fsm.reset(s)
        title = "已拒绝执行" if d.tier == "DENY" else "需人工复核"
        ticket = f"TK{clock.now().strftime('%m%d%H%M%S')}"
        return {"reply": ("这笔我不能替您办。" if d.tier == "DENY" else
                          f"这笔需要人工介入，工单号 {ticket}，客服会在 30 分钟内联系您。"),
                "card": notice("danger" if d.tier == "DENY" else "warn", title,
                                d.reasons + ([f"工单号 {ticket}"] if d.tier == "HANDOFF" else [])),
                "debug": {"intent": intent, "decision": d.to_dict()}}

    if d.tier == "CONFIRM":
        stash_pending(s, plan, out, ctx, d, slots, intent, inj)
        return {"reply": f"这笔需要您确认一下（风险分 {d.risk}）。{('这是不可逆操作，确认后无法撤销。' if d.require_double_confirm else '')}",
                "card": confirm_card(plan, slots, out, d),
                "debug": {"intent": intent, "decision": d.to_dict(),
                          "plan": [st.model_dump() for st in plan.steps]}}

    # ---- AUTO：直接执行 ----
    try:
        executor.run(plan, "commit", trace, out)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code, "compensated": out.compensated}, "error")
        fsm.reset(s)
        return {"reply": e.user_msg, "card": notice("danger", "执行未完成",
                                                     [e.user_msg, *(["已自动回滚"] if out.compensated else [])]),
                "debug": {"intent": intent, "error": e.code, "compensated": out.compensated}}
    text_out, card, extra = build_presentation(intent, plan, out, d, slots)
    all_facts = executor.facts(slots, out.results, extra)
    bad = guard_check(text_out, card, all_facts)
    if bad:
        text_out = "操作已完成，详细结果见下方卡片。"
        audit.append(trace, "guard_rewrite", {"violations": bad}, "ok")
    fsm.reset(s)
    return {"reply": text_out, "card": card,
            "debug": {"intent": intent, "decision": d.to_dict(), "guard_violations": bad,
                      "plan": [st.model_dump() for st in plan.steps]}}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _executed(resp: JSONResponse) -> bool:
    """这次请求是否真的改变了世界（拿到了回执卡）。

    只读问答、以及"还需要再点一次确认"都**不算**——否则第二次「确认」
    会被当成重放直接回放"请再确认一次"，用户永远落不了地。
    """
    try:
        body = json.loads(bytes(resp.body))
        return (body.get("card") or {}).get("type") == "receipt"
    except Exception:                                          # noqa: BLE001
        return False


def handle_chat(req: ChatReq, headers: dict[str, str] | None = None,
                ip: str = "127.0.0.1") -> JSONResponse:
    """对话主入口（不依赖 FastAPI Request）。

    抽出来的原因有两个：
    1. 进程内调用者（评测脚本、其他渠道 adapter）不必伪造一个 Request；
    2. "加渠道只写 adapter"这句话得有地方落——adapter 调这个函数即可。
    """
    t0 = time.time()
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    p = identity.principal_from_headers(hdrs, ip)
    trace = hdrs.get("x-request-id") or uuid.uuid4().hex[:16]

    # 先按**原始请求体**算指纹——客户端没带 session_id 时，如果拿补全后的体去哈希，
    # 每次都会生成新会话、新哈希，重试就永远对不上（等于没有幂等）。
    body = json.dumps(req.model_dump(exclude={"device_trusted"}), ensure_ascii=False,
                      sort_keys=True, default=str).encode()
    req_hash = hashlib.sha256(body).hexdigest()

    # 再取会话，拿到"这次请求作用在哪个待确认事务上"。
    # 只用请求体派生幂等键是不够的：两笔不同业务的「确认」请求体完全一样
    # （都是 {session_id, "确认"}），会被缓存串成同一笔——端到端剧本里
    # 就出现过"确认 8000 元"却执行了上一场的定时转账。
    s0 = fsm.get(req.session_id, p)
    req = req.model_copy(update={"session_id": s0.sid})
    scope = str(s0.stash.get("confirm_id") or s0.state or "IDLE")

    explicit = (hdrs.get("x-idempotency-key") or "").strip()
    if explicit:
        key, ttl = explicit, config.IDEMPOTENCY_TTL
    else:
        key = idem.derive_key(p.uid, p.device_id, body + b"|" + scope.encode())
        ttl = config.IDEMPOTENCY_AUTO_TTL

    slot = idem.begin(p.uid, "chat", key, req_hash, ttl=ttl)
    if slot.state == "replay":
        return JSONResponse(idem.replay_payload(slot), status_code=slot.http_status or 200,
                            headers={"x-idempotent-replay": "true"})
    if slot.state == "inflight":
        return JSONResponse({"ok": False, "error": {
            "code": "IN_PROGRESS", "message": "这笔正在处理，请勿重复提交"}},
            status_code=409, headers={"Retry-After": "1"})
    if slot.state == "conflict":
        return JSONResponse({"ok": False, "error": {
            "code": "IDEMPOTENCY_KEY_REUSED", "message": "同一幂等键对应了不同请求"}},
            status_code=422)

    token = reqctx.begin(p, trace, key)
    try:
        resp = _chat_inner(req, p, trace, t0)
        done = _executed(resp)
        try:
            if done:
                idem.finish(p.uid, "chat", key, resp.status_code, bytes(resp.body))
            else:
                idem.fail(p.uid, "chat", key)     # 没落地：允许同一个 key 再试
            identity.touch_device(p.uid, p.device_id, success=done)
        except Exception:                                     # noqa: BLE001
            # 幂等/设备画像写失败不能把已经成功的业务操作变成 5xx
            log.warning("idem_record_failed", request_id=trace)
        return resp
    except Exception:
        try:
            idem.fail(p.uid, "chat", key)
        except Exception:                                     # noqa: BLE001
            pass
        raise
    finally:
        reqctx.reset(token)


@api.post("/chat")
def chat(request: Request, req: ChatReq):
    return handle_chat(req, dict(request.headers), _client_ip(request))


def _chat_inner(req: ChatReq, p: identity.Principal, trace: str, t0: float):
    s = fsm.get(req.session_id, p)
    # 设备信任由服务端按 (uid, device_id) 历史判定，不再采信客户端自述
    s.device_trusted = identity.device_trusted(p.uid, p.device_id)
    text = (req.text or "").strip()

    scan = injection.scan(text)
    audit.append(trace, "user_input", {"text": text, "session": s.sid,
                                       "injection": scan.to_dict()},
                 "blocked" if scan.level == "high" else "ok")

    out: dict
    # ---------- 确认态 ----------
    if s.state == "AWAIT_CONFIRM":
        if s.expire_pending():
            return JSONResponse(build_response(s, trace, "确认已超时作废，请重新发起。", None,
                                      {"expired": True}, t0))
        if nlu.is_cancel(text):
            fsm.reset(s)
            return JSONResponse(build_response(s, trace, "好的，已取消，这笔不会执行。",
                                      notice("info", "已取消", ["未发生任何资金变动"]),
                                      {"cancelled": True}, t0))
        # 「好的，不过改成 600」：以确认词开头、但金额与待确认的不一致 →
        # 按新金额重走一遍，**绝不能**当成"确认原金额"。
        amt_now = parse_amount(text)
        pend_amt = (s.stash.get("snapshot") or {}).get("amount")
        if amt_now is not None and pend_amt is not None and nlu.starts_with_confirm(text):
            try:
                changed = Decimal(str(amt_now)) != Decimal(str(pend_amt))
            except Exception:                                  # noqa: BLE001
                changed = False
            if changed:
                intent0 = s.stash.get("intent") or "transfer.create"
                slots0 = {**(s.stash.get("slots") or {}), "amount": amt_now}
                fsm.reset(s)
                audit.append(trace, "confirm_amount_modified",
                             {"from": str(pend_amt), "to": str(amt_now)}, "ok")
                res = _handle(s, text, intent0, slots0, trace, scan.level != "clean")
                return JSONResponse({**build_response(s, trace, res["reply"], res["card"],
                                             res.get("debug", {}), t0)})
        if nlu.is_confirm(text):
            if s.stash.get("double") and s.stash.get("confirm_n", 0) < 1:
                s.stash["confirm_n"] = 1
                fsm.save(s)          # 会话已落盘，改了计数必须写回，否则第二次确认会再问一遍
                return JSONResponse(build_response(
                    s, trace, "这是不可逆操作。确认无误请再回复一次「确认」。",
                    notice("warn", "需要二次确认", ["挂失后原卡立即失效，不可撤销"]),
                    {"double_confirm": 1}, t0))
            return JSONResponse(execute_pending(s, trace, t0, p))
        fsm.reset(s)   # 其他输入：先撤销待确认事务，再按新指令处理

    # ---------- 选择态 ----------
    if s.state == "CHOOSE":
        opt = pick_option(s, text)
        if opt is None:
            return JSONResponse(build_response(s, trace, "没听清是哪一个，回个序号（第1个/第2个）就行。",
                                      choose_response(s.stash.get("kind", "item"),
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
                                    label=f"下单「{opt.get('name')}」", on_fail="compensate")])
            res = run_plan(s, plan, "gift.order", plan.slots, trace, scan.level != "clean")
            return JSONResponse({**build_response(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})
        forced = int(opt["id"]) if kind == "payee" else None
        if kind == "card":
            slots = {**slots, "card_last4": str(opt.get("last4") or opt.get("id"))}
            res = _handle(s, text, intent, slots, trace, scan.level != "clean")
            return JSONResponse({**build_response(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})
        res = _handle(s, text, intent, slots, trace, scan.level != "clean", forced)
        return JSONResponse({**build_response(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})

    # ---------- 澄清态 ----------
    if s.state == "CLARIFY":
        intent, slots = merge_clarify(s, text)
        fsm.reset(s)
        res = _handle(s, text, intent, slots, trace, scan.level != "clean")
        return JSONResponse({**build_response(s, trace, res["reply"], res["card"], res.get("debug", {}), t0)})

    # ---------- 注入拦截 ----------
    if scan.level == "high":
        audit.append(trace, "injection_block", scan.to_dict(), "blocked")
        return JSONResponse(build_response(
            s, trace, "这段请求超出了我能代办的范围，换个说法试试？",
            notice("danger", "请求已被拦截", scan.hits[:3]),
            # plan 恒为空数组：拦截发生在规划之前，客户端据此可判断"一步都没执行"
            {"intent": None, "injection": scan.to_dict(), "plan": [], "blocked": True}, t0))

    # ---------- 正常意图 ----------
    p = nlu.parse(text)
    if p["intent"] == "unknown":
        fsm.reset(s)
        if nlu.is_confirm(text):
            return JSONResponse(build_response(
                s, trace, "现在没有待确认的操作——上一笔已经办完了。",
                notice("info", "没有待确认的事", ["可以直接说下一件事"]),
                {"no_pending": True}, t0))
        return JSONResponse(build_response(
            s, trace, "没太听明白。我能办：转账、账单、理财、卡片、订阅，还有跨场景的生日安排。",
            {"type": "notice", "tone": "info", "title": "可以试试这些",
             "lines": ["给我妈转500，备注生活费", "这个月钱都花哪了",
                       "有哪些自动续费", "下周三我妈生日，帮我准备一下"]},
            {"intent": "unknown", "confidence": p["confidence"]}, t0))

    # 否定式：「别给我妈转钱」不能被当成"转多少"的授权
    if nlu.is_negation(text):
        fsm.reset(s)
        audit.append(trace, "negation", {"intent": p["intent"]}, "ok")
        return JSONResponse(build_response(
            s, trace, "好的，这次不执行。",
            notice("info", "已取消", ["没有发起任何操作"]),
            {"intent": p["intent"], "negation": True}, t0))

    res = _handle(s, text, p["intent"], p["slots"], trace, scan.level != "clean")
    dbg = {"intent": p["intent"], "intent_label": nlu.INTENT_LABEL.get(p["intent"]),
           "confidence": p["confidence"], "slots": p["slots"], **res.get("debug", {})}
    return JSONResponse({**build_response(s, trace, res["reply"], res["card"], dbg, t0)})


def run_plan(s: fsm.Session, plan: Plan, intent: str, slots: dict, trace: str, inj: bool) -> dict:
    """给"非 NLU 构造的 plan"（如礼物下单）用的执行入口。"""
    out = executor.Outcome()
    executor.run(plan, "preview", trace, out)
    stats = risk_stats()
    ctx = build_context(plan, slots, out, stats, inj, s)
    d = policy.decide(ctx)
    audit.append(trace, "policy_decision", {"action": ctx.action, "risk": d.risk, "tier": d.tier},
                 d.tier.lower())
    if d.tier in ("DENY", "HANDOFF"):
        fsm.reset(s)
        return {"reply": "这笔我不能替您办。" if d.tier == "DENY" else "这笔需要人工复核。",
                "card": notice("danger" if d.tier == "DENY" else "warn",
                                "已拒绝执行" if d.tier == "DENY" else "需人工复核", d.reasons),
                "debug": {"decision": d.to_dict()}}
    if d.tier == "CONFIRM":
        stash_pending(s, plan, out, ctx, d, slots, intent, inj)
        return {"reply": f"这笔需要您确认（风险分 {d.risk}）。",
                "card": confirm_card(plan, slots, out, d, note="点确认后才会真正执行"),
                "debug": {"decision": d.to_dict()}}
    try:
        executor.run(plan, "commit", trace, out)
    except YinshuError as e:
        audit.append(trace, "execute_failed", {"code": e.code}, "error")
        fsm.reset(s)
        msg = e.user_msg or "执行失败，请稍后再试。"
        return {"reply": msg, "card": notice("danger", "执行未完成", [msg]),
                "debug": {"decision": d.to_dict(), "error": e.code,
                          "compensated": out.compensated}}
    text_out, card, extra = build_presentation(intent, plan, out, d, slots)
    bad = guard_check(text_out, card, executor.facts(slots, out.results, extra))
    fsm.reset(s)
    return {"reply": text_out, "card": card, "debug": {"decision": d.to_dict(), "guard_violations": bad}}


@api.get("/health")
def health():
    bank = {}
    try:
        bank = gateway.call("account.list", {}) and {"ok": True}
    except Exception:
        bank = {"ok": False}
    return {"ok": True, "bank": bank, "audit": audit.stats(),
            "llm": "rule-engine (deterministic)", "policy": policy.config()["tiers"],
            "auth_mode": config.AUTH_MODE, "debug_echo": config.DEBUG_ECHO,
            "anchor": audit.get_anchor().name,
            "version": config.APP_VERSION, "env": config.ENV}


@api.get("/policy")
def get_policy():
    return {"config": policy.config(), "matrix": policy.matrix()}


@api.get("/tools")
def get_tools():
    return {"tools": registry.catalog(), "count": len(registry.TOOLS)}


@api.get("/audit/verify")
def audit_verify(full: bool = False):
    """校验审计链。默认从最近检查点起做增量校验；`?full=true` 强制从头。"""
    return audit.verify(full=full)


@api.get("/audit/{trace_id}")
def audit_trace(trace_id: str):
    return {"trace_id": trace_id, "logs": audit.history(trace_id)}


@api.get("/plan/{text}")
def preview_plan(text: str):
    """调试台用：只出计划，不执行。"""
    p = nlu.parse(text)
    plan = planner.build(p["intent"], p["slots"])
    return {"intent": p["intent"], "slots": jsonable(p["slots"]),
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
    if exc.code in ("POLICY_DENY", "INJECTION_BLOCKED", "SESSION_OWNER_MISMATCH",
                    "UNAUTHORIZED", "IDENTITY_IN_BODY"):
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

# 只注册一份路由；/api/v1/* 的兼容由 VersionAlias 中间件完成
app.include_router(api, prefix="/api")


@app.get("/", include_in_schema=False)
def index():
    f = FRONTEND / "index.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"msg": "前端未找到", "path": str(f)})
