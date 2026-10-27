# 变更记录

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，版本号遵循语义化版本。

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
