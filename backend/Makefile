PY ?= python

.PHONY: install db migrate api worker demo test lint token

install:
	pip install -e ".[dev]"

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

lint:
	ruff check knowledge tests scripts

typecheck:
	mypy

token:
	$(PY) -m knowledge.security issue --subject $${SUBJECT:-dev-user}
