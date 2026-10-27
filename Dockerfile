# 银枢 YinShu —— 生产镜像
# 单镜像同时提供「后端」与「Mock 核心系统」两个入口，用 compose 拆成两个服务。
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY config ./config
COPY frontend ./frontend
COPY start.py demo.py ./

# 非 root 运行；数据目录交给挂载卷
RUN useradd -m -u 10001 yinshu \
    && mkdir -p /app/data \
    && chown -R yinshu:yinshu /app
USER yinshu

ENV YINSHU_ENV=prod \
    YINSHU_HOST=0.0.0.0 \
    YINSHU_DATA_DIR=/app/data \
    YINSHU_SESSION_BACKEND=sqlite

EXPOSE 8000 8010

# 容器内只看存活；就绪由 compose healthcheck 打 /readyz
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
