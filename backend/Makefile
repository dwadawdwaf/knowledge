PY ?= python

.PHONY: install db migrate api worker demo test lint token check frontend-test browser-test

install:
	$(PY) -m pip install -r requirements-dev.lock

# 启动内置 PostgreSQL（含 pgvector），无需另装数据库；前台运行，Ctrl+C 停止
db:
	$(PY) scripts/dev_db.py

migrate:
	alembic upgrade head

api:
	uvicorn knowledge.entrypoints.api:app --reload --port 8010

worker:
	$(PY) -m knowledge.entrypoints.worker

# 端到端演示：建库 → 上传 → 处理 → 检索 → 问答 → 引用
demo:
	$(PY) scripts/demo_flow.py

test:
	pytest -q

# 完整交付验收：锁版本、lint、类型、隔离数据库与 HTTP E2E。
check:
	$(PY) -u scripts/check.py

frontend-test:
	node --check ../frontend/app.mjs
	node --test ../frontend/core.test.mjs

# 需要本机已有 Node.js 与独立测试浏览器，不自动安装。
browser-test:
	$(PY) -u scripts/browser_smoke.py

lint:
	ruff check knowledge deepdoc_sidecar tests scripts

typecheck:
	mypy
	mypy -p deepdoc_sidecar

token:
	$(PY) -m knowledge.security issue --subject $${SUBJECT:-dev-user}
