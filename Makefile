# 银枢 YinShu —— 一键流程
# 注意：Windows 自带环境通常没有 make，请优先用 `python start.py`（等价于 make demo）
# 若已装 make（Git Bash / WSL / mingw32-make），以下目标均可用。

PY ?= python
PIP ?= $(PY) -m pip

.PHONY: help setup data demo prod test cov eval lint smoke docker up down reset clean

help:           ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:          ## 建虚拟环境并装依赖（含开发依赖）
	$(PY) -m venv .venv
	.venv/Scripts/python.exe -m pip install -U pip
	.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt

data:           ## 生成模拟银行数据（12 个月流水 + 通讯录 + 理财货架）
	$(PY) -m backend.mock_bank.seed --reset

demo:           ## 起 Mock 核心系统(8010) + 银枢后端(8000) + 打开浏览器
	$(PY) start.py

prod:           ## 生产模式启动（多 worker + JSON 日志 + 限流）
	$(PY) start.py --prod

test:           ## 跑全部用例
	$(PY) -m pytest -q

cov:            ## 跑用例并出覆盖率报告
	$(PY) -m pytest --cov=backend --cov-report=term-missing --cov-report=html

eval:           ## 出评测报告 docs/04-评测报告.md
	$(PY) -m tests.run_eval

smoke:          ## 起服务后跑 13 场剧本（需要服务已启动）
	$(PY) demo.py --reset

docker:         ## 构建镜像
	docker build -t yinshu:1.0.0 .

up:             ## docker compose 起完整环境
	docker compose up -d --build

down:           ## 停掉 compose 环境
	docker compose down

reset:          ## 一键重建数据库（演示前必跑）
	$(PY) -m backend.mock_bank.seed --reset

clean:          ## 清掉数据库与缓存
	rm -rf data backend/__pycache__ backend/*/__pycache__ tests/__pycache__ \
	       .pytest_cache .coverage htmlcov
