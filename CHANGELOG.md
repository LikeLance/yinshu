# 变更记录

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，版本号遵循语义化版本。

## [1.1.0] — 2026-10-27

按"能否上线"的标准做了一轮加固。核心是**把"说了但没做到"的地方补齐**：
评审发现 6 处会直接击穿卖点的问题（审计链、确认态、幂等、身份、策略配置、限流顺序），
全部修复并配了可复现用例。完整报告见 `docs/review-2026-10-27.md`。

### 修复（都会在真实压力下出事）

- **审计链多进程分叉（P0）**：原实现靠进程内 `threading.Lock` 保护"读尾→插入"，
  而 `--prod` 是多 worker。实测 4 进程 × 50 条产生 **10~11 组分叉**，`verify()` 直接报断链。
  改为 thread-local 双连接 + `BEGIN IMMEDIATE` 跨进程互斥；补 `busy_timeout`；
  `verify()` 改分块校验 + 检查点（不再整表进内存）；加 `chain_ver` 版本分派，旧库原地升级不断链。
- **确认阶段 TOCTOU（P0）**：原实现把 preview 快照落盘，点确认后**直接拿最多 5 分钟前的状态执行**，
  期间余额变化/收款人被拉黑/频率上升全不重算。现在确认时重新 preview + 重算指纹 + 重新决策，
  漂移分 HARD（重建确认卡）/ SOFT（执行并留痕）两级；并用 `UPDATE ... WHERE ver=?` 做跨进程 CAS 防连点。
- **幂等键随请求漂移（P0）**：原来 `Idempotency-Key = trace_id-step_id`，而 trace 每次请求新生成
  → 客户端重试必重复扣款。改为按 `(uid, device, 请求体, 待确认事务)` 派生，或由客户端显式
  `X-Idempotency-Key`；下游 `mock_bank /v1/holds` 补强制幂等头 + 唯一索引。
- **会话即凭证（P0）**：`session_id` 是唯一认证物，拿到一个 `AWAIT_CONFIRM` 的 sid 发"确认"
  就能触发转账。现在会话绑定 `owner_uid/owner_device`，跨主体 403；`device_trusted` 不再采信客户端自述。
- **`policy.json` 的 hard_rules 是摆设（P0）**：`decide()` 把四条硬规则写死在 if 里，
  改 json 完全不生效。改为谓词表解释执行，`when/args/risk/reason` 全部由配置驱动，
  5 万阈值也走 `args`；配置错误在加载时 fail-fast。
- **限流在鉴权内层（P0）**：`ApiKeyGate` 在 `RateLimit` 外面，401 请求永远消耗不到令牌桶。
  中间件顺序改为 `CORS → RequestContext → RateLimit → ApiKeyGate → BodyLimit`。
- **L2 间接注入未接线（P1）**：`injection.scan_untrusted_data` 定义了从没被调用。
  现在工具返回的备注/商户名/产品名都要过一遍，命中即强制升档。
- **资金悬挂（P1）**：`gift.order` 失败后锁定的预算永不释放。补 `on_fail=compensate` +
  补偿器签名扩为 `(out, sid, plan)`。
- **确认词过宽（P1）**：「好的，不过改成 600」会被判成确认并转出原金额。改为整句匹配 +
  金额冲突分支（改成新金额重走，绝不执行原金额）。
- **否定句当授权（P1）**：「别给我妈转钱」原先追问"转多少"。新增否定意图识别。
- **同名同关系静默取第一个（P1）**：与"绝不猜"的承诺不符，改为只要并列就出候选卡。
- **卡片不过幻觉闸（P1）**：原先只校验 `reply`，而卡片上的金额才是用户最信的。

### 新增

- **身份层** `backend/security/identity.py`：标准库手写 HS256 JWT（零新增依赖），
  身份只认 `Authorization` 头（请求体里出现 `user_id` 直接 422）；
  `YINSHU_AUTH_MODE=off|optional|required`，**默认 off**，保证现有演示与测试零改动。
- **服务端幂等** `backend/core/idempotency.py`：只缓存"真的落地了"的响应（回执卡），
  中间态标 failed 允许继续推进；显式 key 24h、派生 key 10min。
- **计划指纹** `backend/planner/fingerprint.py`：对**解析后的真实参数**做哈希
  （只哈希 Plan 不够——`$s1.chosen` 两次解析可能指向不同收款人）。
- **请求级上下文** `backend/core/reqctx.py`：ContextVar 承载主体 / trace / 幂等种子。
- **审计脱敏** `backend/security/pii.py`：`payload` 落库前脱敏，原文只存 HMAC 摘要进 `pii_vault`；
  审计加 `actor_id/device_id/ip/channel` 并纳入哈希（追责维度）。
- **链尾锚定**：`Anchor` 协议 + `LocalTableAnchor` + `HttpAnchor`（接 WORM/存证）；
  `lifespan` 定期锚定，`verify()` 比对锚点，能发现"整表重写 + 本地重算"。
- **进程内入口** `handle_chat(req, headers, ip)`：不依赖 FastAPI 的 `Request`，
  给评测脚本与其他渠道 adapter 用。
- **测试**：105 → **135 条**（新增并发、幂等、TOCTOU、策略配置、检测各一组）；
  另加 `tests/stress_audit_multiproc.py` 多进程验枪工具。

### 变更（可能影响集成方）

- `ChatReq` 改 `extra="forbid"`：请求体里带 `user_id`/`uid` 等未知字段会 **422**。
- `device_trusted` 字段保留但**已忽略**；设备身份改用 `X-Device-Id` 头。
- 幂等键算法变了（`yinshu:v2:...`），**旧的在途幂等键不会命中**，升级时请清在途请求。
- `audit_logs` / `sessions` / `holds` 三张表加列，**有自动迁移，无需重建**；
  但 1.0.0 的老行 `chain_ver=1`，校验时按旧公式处理。
- `_on_yinshu_error` 新增映射：`SESSION_OWNER_MISMATCH` / `UNAUTHORIZED` / `IDENTITY_IN_BODY` → 403。
- 评测报告 `docs/04-评测报告.md` 自带时钟固定声明（14:00），结果不再随运行时刻漂移。

### 可复现性修复

- `--basetemp=.tmp/pytest` 在 pytest 9 + 全新克隆下直接 `FileNotFoundError`（`.tmp/` 被 gitignore），
  6 条用例 ERROR。改由 conftest 导入时创建父目录。
- 权限引擎的"凌晨 0-6 点 +10 分"直接读 wall clock，导致同一句话白天 AUTO、凌晨 CONFIRM，
  13 场剧本白天 13/13、凌晨 11/13。新增 `backend/core/clock.py` + `YINSHU_FIXED_HOUR`
  （仅非 prod，只钉小时不动日期），测试与评测均固定时钟。

### 第二批：架构拆分 + HTTP 层加固 + 输入鲁棒性

- **`main.py` 拆分**：领域逻辑下沉到 `backend/services/`（`view` / `context` / `confirm` /
  `choose`），`main.py` 1108 → 692 行，只剩 HTTP 编排（路由、状态机分派、运维接口、
  异常处理、装配）。`services/` 层**不 import fastapi**，可被评测脚本与渠道 adapter 复用。
  为此把 `_jsonable/_notice/_card_text/_guard_check/_resp…` 等改名为公开名
  （`jsonable/notice/card_text/guard_check/build_response…`）。
- **版本化入口改成中间件**：`/api` 与 `/api/v1` 原先双 `include_router`，
  而 FastAPI 0.162 下 `include_in_schema=False` 不生效 → OpenAPI 每个接口重复一份、
  还刷 Duplicate Operation ID 警告。新增 `VersionAlias` 中间件在进路由前改写路径，
  路由只注册一份，文档 14 → 7 条。
- **`BodyLimit` 补 chunked 绕过**：只信 `Content-Length` 时，分块传输可以无限灌；
  现在长度缺失就边读边数，超限立刻 413。
- **跨境判定数据化**：`"HSBC" in bank` 这种猜法既会漏（内地汇丰）也会误伤；
  改为核心系统给出的 `payees.cross_border` 标记（入参加列式迁移，旧库自动补）。
- **输入归一化 `backend/nlu/normalize.py`**：NFKC（全角数字）+ 去零宽/双向控制字符 +
  去 emoji + 空白折叠。emoji、全角、粘贴带进来的不可见字符不再让规则失配。
  **不做**繁简转换（半套词表比不做更危险），登记为已知缺口。
- **规则补两处**：转账动词支持「打/转/汇/付 + 数字」；问句形态
  （有什么/有哪些/推荐/介绍）优先归到理财对比，不急着下申购单。
- **扰动集**：新增 `tests/cases/perturb.jsonl`（24 条：emoji/全角/零宽/空格/同义动词），
  `run_eval` 起单列一栏并在报告里声明「这不是留出集」——作者写完据其修了两条规则。
- **测试**：135 → **168 条**（新增 `test_http_hardening.py` 5 条、`test_nlu_robustness.py` 28 条）。

### 第三批：多租户隔离（身份贯穿到核心系统）

- **身份只从请求上下文来**：Mock 核心新增 `Identity` 中间件，从 `X-User-Id` 注入
  ContextVar；原先 `?user_id=1` 是可伪造的查询参数，传别人的 id 就能读别人的账户。
- **读写全部按主体隔离**：账户 / 收款人 / 转账 / 账单 / 理财持仓 / 卡片 / 订阅 / 锁资金 /
  礼物订单全部按 `uid()` 过滤；`wealth_order`、`wealth_redeem`、`holds`、`gift_orders`
  原先写死 `user_id=1`，现在按调用方落库。
- **归属校验失败关闭**：动钱或改状态前校验资源是否属于调用方，不属于直接 403
  （账户 / 收款人 / 卡片 / 持仓 / 订阅 / 资金锁定）。
- **账户 per-user 解析**：`planner.build(intent, slots, account_id)`；
  新增 `services.context.account_of()` 按主体取默认活期账户，
  取代写死的 `ACCOUNT_ID = 1`。账单类不再给 `account_id=1` 默认值，
  不传时由核心系统按调用方取默认账户。
- **第二个客户**：种子数据新增「李雷」（独立账户 / 卡 / 收款人），
  用来验证隔离——它不是演示脚本里的角色，只服务于测试与真实多用户场景。
- **测试**：178 条（新增 `test_multitenant.py` 10 条：读隔离 / `?user_id=` 冒充无效 /
  跨用户账户·收款人·卡 403 / 自有转账只动自有资金 / 规划器按主体取账户 /
  缺省鉴权下会话不能被他人接手）。

修复（这批里被测试撞出来的两个真 bug）：

- **网关 GET 请求丢请求头**：`X-User-Id` / `X-Trace-Id` 只在 POST/PATCH 上发送，
  GET 分支漏了 —— 导致"所有读请求都退化成用户 1"。多租户一上线就暴露了这个洞。
- **幂等键可能含非 ASCII**：`X-Request-Id` 由客户端提供，拼进 `Idempotency-Key`
  会抛 `UnicodeEncodeError` → 500。现在做 ASCII 降级。
- **`account_of()` 过宽的 except**：`TypeError` 也被吞掉，会让"少传一个参数"
  静默退化成"用兜底账户（用户 1 的）"。现在只兜下游不可用。

## [1.0.0] — 2026-10-27

从"能跑的 demo"升级为"可交付级产品"。核心是**可运维、可对接、可审计**。

### 新增

- **会话持久化**：`backend/session/store.py`，SQLite(WAL) 落盘；`Plan/Outcome/Decision` 带类型序列化，
  金额用 `Decimal` 编解码（不落浮点）。**进程被 kill 后重启，待确认事务仍可执行**（已实测）。
- **可观测性**：`backend/core/logging.py`（JSON/文本双格式、字段级结构化）、`backend/core/metrics.py`
  （Prometheus 文本输出）、`x-request-id` 与 `x-response-time-ms` 贯穿、访问日志。
- **中间件**：令牌桶限流（429 + `Retry-After`）、请求体 64KB 上限（413）、可选 `X-API-Key` 鉴权（401）。
- **API 契约**：路由迁到 `APIRouter`，同时挂 `/api` 与 `/api/v1`；统一错误体 `{ok:false,error:{code,message,request_id}}`；
  `debug` 三件套（`guard_violations`/`plan`/`state`）在任何分支都齐备。
- **健康探针**：`/healthz`（存活）与 `/readyz`（就绪，核心系统不通返回 503）分离。
- **部署形态**：`Dockerfile`（多阶段、非 root、内置 HEALTHCHECK）、`docker-compose.yml`（后端+Mock 核心+数据卷+依赖健康）、
  `.dockerignore`；`start.py --prod`（多 worker）、`.env` 自动加载。
- **工程化**：`pyproject.toml`（pytest/coverage 配置）、`requirements-dev.txt`、`.gitignore`、
  `.github/workflows/ci.yml`（测试→覆盖率门槛→剧本→评测→镜像构建）。
- **文档**：`docs/00-交付清单.md`、`docs/02-安全设计.md`（安全自评报告）、`docs/03-API契约.md`、
  `docs/05-运维手册.md`、`CHANGELOG.md`、`LICENSE`。
- **测试**：从 12 条扩到 **105 条**（会话存储、网关、权限引擎、中间件、接口契约、全意图矩阵、受控流程），
  后端覆盖率 **82%**。

### 修复

- **幂等重放返回结构不一致**：Mock 核心系统重放时返回裸数据库行（`id` 而非 `transfer_id`），
  客户端按一套 schema 解析会在重试时崩；现与首次响应同构。
- **审计写入是性能瓶颈**：默认日志模式每次 `commit` 都 fsync，Windows 上单请求 ~80ms；
  改 WAL + `synchronous=NORMAL` 后降到 4~5ms（16 倍）。
- **网关每次新建连接**：改为共享 `httpx.Client` 连接池（keep-alive），并统一 `trust_env=False` 防系统代理劫持。
- **业务异常被当成 500**：查无此收款人等 `YinshuError` 原会冒泡成 HTTP 500；
  现在预览与执行两段都捕获，转成对话式提示，用户可继续操作。
- **日志适配器参数冲突**：`log.warning("...", msg=...)` 与位置参数 `msg` 撞名导致异常处理自身抛错；
  改为位置限定参数。
- **只读请求也写会话库**：改为状态/暂存为空时跳过落盘；`reset()` 直接删行，避免"幽灵事务"。
- **双确认计数丢失**：会话落盘后，`confirm_n` 的内存改动未写回，导致二次确认被重复询问。
- 前端 `?m=` 深链只认 hash（现同时支持 search）；notice 卡片标题与正文重复；CLI 回执误标 `[CONFIRM]`。

### 变更

- 卡片查询按 `card_last4` 定向作答；查不到的尾号不再落到"商户名/产品名"提示，而是说明并列出已有卡片。
- 不可逆/限额类卡片操作（挂失/冻结/调额）：名下多卡且未指定时**先问哪张，一步不执行**。
- 无待确认事务时说「确认」→ 明确回「现在没有待确认的事」，而非"没听太听懂"。
- 订阅已取消后再取消 → 「名下已没有这项在生效的自动续费」。
- `start.py` 支持 `--prod`、`--workers N`、`.env`；`Makefile` 目标补齐（`prod/cov/up/down/docker/help`）。

## [0.1.0] — 2026-10-26

首个可运行版本（内部演示）。

- 五层架构落地：渠道 / 编排 / 工具网关 / 安全横切 / Mock 核心。
- 六大场景端到端可演示；13 场演示剧本。
- 代码级权限引擎（AUTO/CONFIRM/HANDOFF/DENY + 硬规则 + 注入降级）。
- 四层注入防御、`$ref` 引用绑定 + 事实集校验、审计哈希链、幂等键、补偿事务。
- 单文件手机银行 UI + 同屏决策面板；评测报告自动生成。
