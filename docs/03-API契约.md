# 银枢 YinShu · API 契约

> 版本：v1
>
> `/api` 与 `/api/v1` 行为一致，新接入建议使用 `/api/v1`。
>
> 交互式文档：`http://127.0.0.1:8000/docs`

## 一、通用约定

- API 响应默认使用 `application/json; charset=utf-8`。
- 金额使用字符串表示，保留两位小数。
- `/metrics` 返回 Prometheus 文本，`/` 返回前端 HTML，不使用 JSON。
- 响应包含 `x-request-id` 和 `x-response-time-ms`。
- 业务结果通常使用 HTTP 200 + 对话回复；鉴权、限流、参数校验、幂等冲突等协议级错误使用对应 HTTP 状态码。

协议错误示例：

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
| 401 | `UNAUTHORIZED` | 配置了鉴权但请求头缺失或错误 |
| 403 | `SESSION_OWNER_MISMATCH` | 会话不属于当前主体 |
| 403 | `POLICY_DENY` / `INJECTION_BLOCKED` | 策略拒绝或注入拦截 |
| 404 | `NOT_FOUND` | 路径不存在 |
| 409 | `IN_PROGRESS` | 同一幂等键正在处理 |
| 409 | `LIMIT_EXCEEDED` / `INSUFFICIENT_FUNDS` | 限额或余额错误 |
| 413 | `BODY_TOO_LARGE` | 请求体超过 64KB |
| 422 | `INVALID_REQUEST` | 参数校验失败，包括请求体中的未知身份字段 |
| 422 | `IDEMPOTENCY_KEY_REUSED` | 同一幂等键用于不同请求体 |
| 429 | `RATE_LIMITED` | 超过限流，响应头包含 `Retry-After` |
| 500 | `INTERNAL_ERROR` | 未预期异常，堆栈只写入日志 |

### 请求头

| 头 | 必需 | 说明 |
|---|---|---|
| `Authorization: Bearer <JWT>` | `AUTH_MODE=required` 时必需 | HS256，载荷包含 `sub`、`did`、`ch`、`iat`、`exp`、`jti` |
| `X-Idempotency-Key` | 资金类操作建议提供 | 客户端业务单号；同 key 同请求只执行一次 |
| `X-Device-Id` | 建议 | 设备标识，用于服务端设备判定，不代表可信 |
| `X-Request-Id` | 可选 | 透传到 `trace_id` 和审计记录 |
| `X-API-Key` | 配置 API Key 时必需 | 由 `YINSHU_API_KEYS` 配置 |

未提供 `X-Idempotency-Key` 时，服务端按 `(uid, 设备, 请求体, 待确认事务)` 派生兜底键，默认窗口 10 分钟。需要跨请求的业务幂等时，应显式提供幂等键。

## 二、对话接口

### `POST /api/v1/chat`

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string，最长 64 | 否 | 不传则新建会话 |
| `text` | string，最长 500 | 是 | 用户输入 |
| `device_trusted` | bool | 否 | 兼容字段，服务端已忽略 |

身份由 `Authorization` 头或服务端演示配置提供。请求体出现 `user_id`、`uid` 等未知字段会返回 422。

响应示例：

```json
{
  "session_id": "d1db7a053ca6",
  "trace_id": "9f2c1e0b7a4d4c88",
  "state": "AWAIT_CONFIRM",
  "reply": "这笔需要您确认一下（风险分 35）。",
  "card": {
    "type": "confirm",
    "title": "请确认这笔操作",
    "rows": [["收款人", "李慧（尾号 3921）"]],
    "reasons": ["基础风险：transfer.create（20 分）", "单笔金额超过 5,000 元"],
    "risk": 35,
    "tier": "CONFIRM",
    "need_double": false
  },
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

`state` 取值：

- `IDLE`：可接受新请求；
- `CLARIFY`：等待补充槽位；
- `CHOOSE`：等待选择候选；
- `AWAIT_CONFIRM`：等待确认。

`debug` 包含 `guard_violations`、`plan` 和 `state`。该字段用于调试，生产渠道不应依赖其内部结构；当前实现仍会返回该字段。

### 卡片类型

| 类型 | 含义 | 主要字段 |
|---|---|---|
| `receipt` | 已执行回执 | `title`、`rows` |
| `confirm` | 待确认 | `rows`、`reasons`、`risk`、`tier`、`need_double` |
| `plan` | 计划预览 | `steps` |
| `choose` | 候选选择 | `kind`、`options[{id,label,sub}]` |
| `bill` | 账单结构 | `total`、`count`、`categories`、`top`、`mom` |
| `anomaly` | 异常清单 | `items[{ts,merchant,amount,rule}]` |
| `wealth` | 理财对比 | `products[{name,annual_rate,risk,min_amount}]` |
| `subs` | 订阅清单 | `annual_total`、`items` |
| `cards` | 卡片列表 | `items[{last4,brand,limit,status}]` |
| `balance` | 余额 | `rows` |
| `notice` | 提示、拦截或追问 | `tone`、`title`、`lines` |

未知卡片类型由前端降级为 JSON 展示。

### 会话推进示例

```text
POST /chat {"text":"帮我转8000"}
  → state=CLARIFY，提示补充收款人

POST /chat {"session_id":S,"text":"给我妈"}
  → state=AWAIT_CONFIRM，返回确认卡

POST /chat {"session_id":S,"text":"确认"}
  → state=IDLE，返回回执

POST /chat {"session_id":S,"text":"取消"}
  → 撤销待确认事务，不产生资金变动
```

## 三、运维与调试接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针，不检查依赖 |
| GET | `/readyz` | 就绪探针，核心系统不可用时返回 503 |
| GET | `/metrics` | Prometheus 文本指标 |
| GET | `/api/v1/health` | 银行连通、审计统计、策略阈值、版本和环境 |
| GET | `/api/v1/policy` | 策略配置和决策矩阵 |
| GET | `/api/v1/tools` | 工具白名单与风险元数据 |
| GET | `/api/v1/audit/verify` | 增量校验哈希链 |
| GET | `/api/v1/audit/verify?full=true` | 全量校验哈希链 |
| GET | `/api/v1/audit/{trace_id}` | 查询指定 trace 的审计记录 |
| GET | `/api/v1/plan/{text}` | 只生成计划，不执行 |

### 指标

| 指标 | 类型 | 标签 |
|---|---|---|
| `yinshu_http_requests_total` | counter | `method`、`path`、`status` |
| `yinshu_http_request_seconds` | histogram | 路由与状态 |
| `yinshu_errors_total` | counter | `code` |
| `yinshu_rate_limited_total` | counter | `path` |
| `yinshu_rejected_total` | counter | `reason` |
| `yinshu_uptime_seconds` | gauge | — |

## 四、业务错误码

业务错误通常以对话文本或 `notice` 卡片返回。

| code | 含义 | 用户提示示例 |
|---|---|---|
| `PAYEE_NOT_FOUND` | 通讯录无此人 | 没找到这个人，可以补充姓氏或手机号后四位 |
| `NLU_LOW_CONF` | 意图置信度低 | 没太听明白。可以办理转账、账单、理财等业务 |
| `SLOT_MISSING` | 缺少必填槽位 | 转给谁？ |
| `TOOL_ARG_ERROR` | 工具参数校验失败 | 请求参数不完整 |
| `LIMIT_EXCEEDED` | 超过单笔或日累计限额 | 超过单笔限额 |
| `INSUFFICIENT_FUNDS` | 余额不足 | 余额不足，可用余额为 X 元 |
| `POLICY_DENY` | 策略拒绝 | 收款人命中黑名单或适当性不符 |
| `POLICY_HANDOFF` | 需要人工复核 | 需人工介入，工单号为 TK… |
| `INJECTION_BLOCKED` | 注入拦截 | 该请求超出可代办范围 |

## 五、版本策略

- `v1` 内只做向后兼容变更，新增字段或卡片类型；
- 破坏性变更使用新版本路径；
- `debug` 字段仅供调试，生产前端不应依赖其内部结构；
- 卡片渲染按 `card.type` 分支，未知类型降级为 JSON。
