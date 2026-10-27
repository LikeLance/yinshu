"""全局配置。全部走环境变量，默认值保证「离线 + 零配置」也能完整跑起来。

分三档（YINSHU_ENV）：
  dev   —— 本机开发：热重载、日志给人看、限流宽松
  demo  —— 现场演示：单进程、确定性、限流关闭（防止误伤演示）
  prod  —— 交付部署：多 worker、JSON 日志、限流开启、错误不外泄
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("YINSHU_DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

BANK_DB = DATA_DIR / "bank.db"
APP_DB = DATA_DIR / "app.db"
SESSION_DB = DATA_DIR / "session.db"
POLICY_FILE = Path(os.getenv("YINSHU_POLICY_FILE") or (ROOT / "config" / "policy.json"))

ENV = os.getenv("YINSHU_ENV", "demo").strip().lower()
IS_PROD = ENV == "prod"

BANK_BASE = os.getenv("YINSHU_BANK_BASE", "http://127.0.0.1:8010")
BACKEND_HOST = os.getenv("YINSHU_HOST", "127.0.0.1")
BACKEND_PORT = int(os.getenv("YINSHU_PORT", "8000"))
BANK_PORT = int(os.getenv("YINSHU_BANK_PORT", "8010"))
WORKERS = int(os.getenv("YINSHU_WORKERS", "1"))

# 会话存储：sqlite（默认，重启不丢）/ memory（测试用）
SESSION_BACKEND = os.getenv("YINSHU_SESSION_BACKEND", "sqlite").strip().lower()
SESSION_TTL = int(os.getenv("YINSHU_SESSION_TTL", str(30 * 60)))
CONFIRM_TTL = int(os.getenv("YINSHU_CONFIRM_TTL", str(5 * 60)))

# 限流：每分钟每个来源的请求数；demo 档默认不拦，prod 档默认 120
_default_rpm = {"dev": 600, "demo": 0, "prod": 120}.get(ENV, 120)
RATE_LIMIT_RPM = int(os.getenv("YINSHU_RATE_LIMIT_RPM", str(_default_rpm)))
MAX_TEXT_LEN = int(os.getenv("YINSHU_MAX_TEXT_LEN", "500"))
MAX_BODY_BYTES = int(os.getenv("YINSHU_MAX_BODY_BYTES", str(64 * 1024)))

# 跨域白名单（逗号分隔；* 表示全放开，仅演示环境建议）
CORS_ORIGINS = [o.strip() for o in os.getenv("YINSHU_CORS", "*").split(",") if o.strip()]

# 接入鉴权：配置后 /api/* 必须带 X-API-Key；留空则不校验（演示档默认留空）
API_KEYS = [k.strip() for k in os.getenv("YINSHU_API_KEYS", "").split(",") if k.strip()]
AUTH_EXEMPT = ("/", "/healthz", "/readyz", "/metrics", "/docs", "/openapi.json", "/redoc")

# 可观测性
LOG_LEVEL = os.getenv("YINSHU_LOG_LEVEL", "INFO").upper()
LOG_JSON = os.getenv("YINSHU_LOG_JSON", "0" if ENV == "dev" else "1") == "1"
METRICS_ENABLED = os.getenv("YINSHU_METRICS", "1") == "1"

# LLM 适配器（可选）。不填 key 时全部走规则引擎，功能完整、结果确定。
LLM_API_KEY = os.getenv("YINSHU_LLM_KEY", "").strip()
LLM_BASE = os.getenv("YINSHU_LLM_BASE", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.getenv("YINSHU_LLM_MODEL", "deepseek-chat")
LLM_ENABLED = bool(LLM_API_KEY)

# 身份：off=不校验（演示/测试，身份取 DEMO_USER_ID）/ optional=带 token 则校验 /
# required=必须带 token。默认 off 是为了让现有演示与测试无需改动即可运行。
AUTH_MODE = os.getenv("YINSHU_AUTH_MODE", "off").strip().lower()
JWT_SECRET = os.getenv("YINSHU_JWT_SECRET", "yinshu-dev-secret-change-me").encode()
JWT_TTL = int(os.getenv("YINSHU_JWT_TTL", str(30 * 60)))
# demo 主体绑定的设备号（off 模式下所有请求共用一个，保证会话归属校验恒通过）
DEMO_DEVICE = os.getenv("YINSHU_DEMO_DEVICE", "demo-device")

# 审计链尾锚定：留空则用本地 audit_anchors 表（等价自签），
# 生产应指向行内存证 / WORM 对象锁存储
ANCHOR_URL = os.getenv("YINSHU_ANCHOR_URL", "").strip()
ANCHOR_TOKEN = os.getenv("YINSHU_ANCHOR_TOKEN", "").strip()
ANCHOR_KEY = os.getenv("YINSHU_ANCHOR_KEY", "yinshu-anchor-key-change-me")
ANCHOR_INTERVAL = int(os.getenv("YINSHU_ANCHOR_INTERVAL", "60"))
ANCHOR_EVERY_ROWS = int(os.getenv("YINSHU_ANCHOR_EVERY_ROWS", "200"))

# debug 回吐：prod 默认关闭，避免把内部策略权重、工具名、收款人候选漏给前端
DEBUG_ECHO = os.getenv("YINSHU_DEBUG_ECHO", "0" if IS_PROD else "1") == "1"

# 服务端幂等：客户端可用 X-Idempotency-Key 显式指定；未指定时按
# (uid, device, 请求体) 派生，保证"网络重试"不会变成"重复转账"
IDEMPOTENCY_TTL = int(os.getenv("YINSHU_IDEMPOTENCY_TTL", str(24 * 3600)))
IDEMPOTENCY_AUTO_TTL = int(os.getenv("YINSHU_IDEMPOTENCY_AUTO_TTL", "600"))
IDEMPOTENCY_INFLIGHT_TTL = int(os.getenv("YINSHU_IDEMPOTENCY_INFLIGHT_TTL", "30"))

DEMO_USER_ID = int(os.getenv("YINSHU_USER_ID", "1"))
APP_VERSION = "1.1.0"
