# 银枢 YinShu · API 契约

> 版本 `v1`（`/api` 与 `/api/v1` 行为完全一致，新接入请用 `/api/v1`）
> 交互式文档：`http://127.0.0.1:8000/docs`　OpenAPI：`/openapi.json`

## 一、通用约定

- 全部 `application/json; charset=utf-8`；金额一律**字符串**（避免浮点），两位小数。
- 每个响应都有 `x-request-id`（可透传，未传则生成）与 `x-response-time-ms`。
- 业务错误不抛 HTTP 5xx，统一 200 + 对话式回复；**协议级**错误用下列错误体。

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_REQUEST",
    "message": "参数不合法：text Input should be a valid string",
    "request_id": "84afe3c67bb6413f"
  }
}
```

| HTTP | code | 触发场景 |
|---|---|---|
| 401 | `UNAUTHORIZED` | 配置了 `YINSHU_API_KEYS` 但未带/带错 `X-API-Key`；或 `AUTH_MODE=required` 而缺少/带错令牌 |
| 403 | `SESSION_OWNER_MISMATCH` | 会话不属于当前主体（防止拿别人的 sid 替他确认） |
| 403 | `IDENTITY_IN_BODY` | 请求体里出现身份字段（身份只能来自 `Authorization` 头） |
| 404 | `NOT_FOUND` | 路径不存在 |
| 409 | `IN_PROGRESS` | 同一幂等键的请求正在处理中（重复提交） |
| 413 | `BODY_TOO_LARGE` | 请求体超过 64KB |
| 422 | `INVALID_REQUEST` | 参数校验失败（含 `ChatReq` 的未知字段） |
| 422 | `IDEMPOTENCY_KEY_REUSED` | 同一个 `X-Idempotency-Key` 被用在了不同的请求体上 |
| 429 | `RATE_LIMITED` | 超限流，响应头含 `Retry-After` |
| 500 | `INTERNAL_ERROR` | 未预期异常（堆栈只进日志） |

### 安全相关请求头（v1.1）

| 头 | 必需 | 说明 |
|---|---|---|
| `Authorization: Bearer <JWT>` | `AUTH_MODE=required` 时必需 | HS256；载荷含 `sub`(uid) / `did`(设备) / `ch`(渠道) / `iat` / `exp` / `jti`。**身份只认这个头**，请求体里的 `user_id` 会被 422 拒绝 |
| `X-Idempotency-Key` | 资金类操作建议必带 | 客户端生成的业务单号。同一个 key + 同一请求体只会执行一次，重放返回首次响应（带 `x-idempotent-replay: true`）；同 key 不同体会 422 |
| `X-Device-Id` | 建议 | 设备标识，仅用于服务端做设备画像（是否非常用设备）；**不代表可信**，可信由服务端按历史判定 |
| `X-Request-Id` | 可选 | 透传到 `trace_id` 与审计，便于跨系统串联 |

> 未带 `X-Idempotency-Key` 时服务端会按 `(uid, 设备, 请求体, 待确认事务)` 派生一个兜底 key，
> 窗口 10 分钟，只覆盖网络重试；**真正的业务幂等请显式带 key**。

---

## 二、核心接口：对话

### `POST /api/v1/chat`

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string ≤64 | 否 | 不传则新建，后续带上以延续上下文 |
| `text` | string ≤500 | 是 | 用户原话（中文自然语言） |
| `device_trusted` | bool | 否 | 设备可信信号，仅影响本次决策打分 |

**响应**

```json
{
  "session_id": "d1db7a053ca6",
  "trace_id": "9f2c1e0b7a4d4c88",
  "state": "AWAIT_CONFIRM",
  "reply": "这笔需要您确认一下（风险分 35）。",
  "card": { "type": "confirm", "title": "请确认这笔操作", "rows": [["收款人","李慧（尾号 3921）"]],
            "reasons": ["基础风险：transfer.create（20 分）","单笔金额超过 5,000 元"],
            "risk": 35, "tier": "CONFIRM", "need_double": false },
  "debug": {
    "intent": "transfer.create",
    "intent_label": "智能转账",
    "confidence": 0.9,
    "slots": {"amount": "8000.00", "payee_raw": "我妈"},
    "decision": {"tier": "CONFIRM", "risk": 35, "reasons": ["..."], "hard_rule": ""},
    "plan": [{"id": "s1", "tool": "account.list"}, {"id": "s2", "tool": "payee.search"}],
    "guard_violations": [],
    "audit": {"id": 1418, "seq_hash": "c86747a7db1c", "ts": "2026-10-27 21:43:21"},
    "latency_ms": 5
  }
}
```

**`state` 取值**：`IDLE` / `CLARIFY`（等补槽）/ `CHOOSE`（等选候选）/ `AWAIT_CONFIRM`（等确认）
**`debug` 三件套恒存在**：`guard_violations`（幻觉违规列表）、`plan`（本次计划，拦截时为空数组）、`state`。

### 卡片类型（`card.type`）

| 类型 | 含义 | 关键字段 |
|---|---|---|
| `receipt` | 已执行回执 | `rows`（键值对）、`title` |
| `confirm` | 待确认 | `rows`、`reasons`、`risk`、`tier`、`need_double` |
| `plan` | 计划预览 | `steps` |
| `choose` | 候选选择 | `kind`、`options[{id,label,sub}]` |
| `bill` | 账单结构 | `total`、`items[{category,amount,ratio}]`、`mom` |
| `anomaly` | 异常清单 | `items[{ts,merchant,amount,rule}]` |
| `wealth` | 理财对比 | `products[{name,annual_rate,risk,min_amount}]` |
| `subs` | 订阅清单 | `annual_total`、`items` |
| `cards` | 卡片列表 | `items[{last4,brand,limit,status}]` |
| `balance` | 余额 | `rows` |
| `notice` | 提示/拦截/追问 | `tone`、`title`、`lines` |

### 会话推进示例

```
POST /chat {"text":"帮我转8000"}                     → state=CLARIFY, notice「转给谁？」
POST /chat {"session_id":S,"text":"给我妈"}          → state=AWAIT_CONFIRM, confirm 卡
POST /chat {"session_id":S,"text":"确认"}            → state=IDLE, receipt 卡
POST /chat {"session_id":S,"text":"取消"}            → 撤销，不产生资金变动
```

---

## 三、运维与调试接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针，不查依赖，恒 200 |
| GET | `/readyz` | 就绪探针，核心系统不通返回 503 |
| GET | `/metrics` | Prometheus 文本格式指标 |
| GET | `/api/v1/health` | 详情：bank 连通、审计统计、策略阈值、版本、环境 |
| GET | `/api/v1/policy` | 策略配置 + 决策矩阵 |
| GET | `/api/v1/tools` | 工具白名单（名称/参数/是否资金类/是否可逆） |
| GET | `/api/v1/audit/verify` | 校验哈希链，返回 `{ok,total,tail}` |
| GET | `/api/v1/audit/{trace_id}` | 某次请求的完整审计轨迹 |
| GET | `/api/v1/plan/{text}` | 只出计划不执行（调试台用） |

### 指标口径

| 指标 | 类型 | 标签 |
|---|---|---|
| `yinshu_http_requests_total` | counter | `method`、`path`（路由模板）、`status` |
| `yinshu_http_request_seconds` | histogram | 固定桶 50ms~10s |
| `yinshu_errors_total` | counter | `code` |
| `yinshu_rate_limited_total` | counter | `path` |
| `yinshu_rejected_total` | counter | `reason`（`body_too_large` / `unauthorized`）|
| `yinshu_uptime_seconds` | gauge | — |

---

## 四、错误码（业务层，HTTP 200 + 对话回复）

| code | 含义 | 用户可见话术 |
|---|---|---|
| `PAYEE_NOT_FOUND` | 通讯录无此人 | 「没找到这个人，试试加上姓氏或手机号后四位？」 |
| `NLU_LOW_CONF` | 意图置信度过低 | 「没太听明白。我能办：转账、账单、理财…」 |
| `SLOT_MISSING` | 缺必填槽位 | 「转给谁？」+ 可答示例 |
| `TOOL_ARG_ERROR` | 网关参数校验失败 | 「请求参数不完整。」 |
| `LIMIT_EXCEEDED` | 超单笔/日累计限额 | 「超单笔限额 50000.00 元」 |
| `INSUFFICIENT_FUNDS` | 余额不足 | 「余额不足，可用余额 X 元」 |
| `POLICY_DENY` | 硬规则拒绝 | 拒绝原因（黑名单/适当性） |
| `POLICY_HANDOFF` | 需人工复核 | 「需人工介入，工单号 TK…」 |
| `INJECTION_BLOCKED` | 注入拦截 | 「这段请求超出了我能代办的范围」 |

---

## 五、兼容性与变更策略

- `v1` 内只做**向后兼容**变更（新增字段、新增卡片类型）；破坏性变更走 `/api/v2`。
- `debug` 为调试用，字段可能增加，**不建议**生产前端依赖其内部结构。
- 卡片渲染建议以 `card.type` 分支，未知类型降级为 JSON 展示（前端已这样实现）。
