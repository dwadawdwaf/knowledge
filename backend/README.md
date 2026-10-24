# 后端服务：知识检索与智能问答助手

实现规格见 [../docs/design.md](../docs/design.md)，跑起来与排障见 [../docs/runbook.md](../docs/runbook.md)。

## 快速开始（不需要安装 PostgreSQL）

依赖里自带 `embedded-postgres`（PostgreSQL 18.6 + pgvector 0.8.6），开发时用它起一个本地库。

```bash
cd backend
pip install -e ".[dev]"

# 终端 1：本地数据库（前台运行，Ctrl+C 停止）
python scripts/dev_db.py
# 输出示例：postgresql+psycopg://postgres:@127.0.0.1:12477/postgres
# 连接串写入 .data/db_url，测试库写入 .data/db_test_url

# 终端 2：迁移 + API
export KS_DATABASE_URL=$(cat .data/db_url)
export KS_STORAGE_ROOT="./.data/objects"
alembic upgrade head
uvicorn knowledge.entrypoints.api:app --port 8010

# 终端 3：Worker
export KS_DATABASE_URL=$(cat .data/db_url) KS_STORAGE_ROOT="./.data/objects"
python -m knowledge.entrypoints.worker

# 终端 4：端到端演示
python scripts/demo_flow.py
```

已有 PostgreSQL 时跳过第一步，把 `KS_DATABASE_URL` 指向自己的库即可；数据库需启用 `vector` 与 `pg_trgm`（迁移会自动创建）。

> Windows 注意：`dev_db.py` 的数据目录必须是纯英文路径（默认在临时目录），且启动过 PostgreSQL 的终端不要用系统代理，否则本地请求会被代理截走。

## 签发开发令牌

```bash
python -m knowledge.security issue --subject dev-user
```

请求带 `Authorization: Bearer <token>`。令牌密钥取自 `KS_TOKEN_SECRET`。

## 目录

```
knowledge/
  entrypoints/   进程入口（api / worker / mcp）
  api/           路由、请求校验、响应组装、异常映射
  mcp/           MCP 工具面（协议实现待 M2）
  services/      用例编排与事务边界
  domain/        实体、枚举、分块算法、端口、错误
  repositories/  SQL 与表定义
  adapters/      解析、向量、生成、存储
  worker/        任务领取循环
scripts/         本地数据库与端到端演示
```

依赖方向：`entrypoints → {api, mcp, worker} → services → domain ← repositories / adapters`。

## 测试

```bash
pytest -q                                                   # 单元测试，不需要数据库
KS_TEST_DATABASE_URL=$(cat .data/db_test_url) pytest -q     # 含集成用例（需 dev_db.py 在跑）
make lint && make typecheck                                 # ruff + mypy
```

集成用例覆盖验收用例 M01–M08：上传→处理→检索→引用、引用回溯源文件位置、新版本处理中旧版本可见、崩溃恢复不重复分块、处理中删除不复活、同字节去重与同名新版本；另含孤儿对象回收用例。

## 效果评测

`testdata/corpus/` 是评测语料（13 份文档、约 490 个分块），两类来源：

- **自制**：内部制度、新员工 FAQ、英文远程办公制度、运行期生成的 DOCX/PDF（内容定义在 `labels.py`）；
- **网络抓取**（经用户授权）：五部法律法规（劳动法、劳动合同法、数据安全法、网络安全法、住房公积金管理条例），来源维基文库，官方文件依《著作权法》第五条不适用著作权保护；由 `python scripts/fetch_corpus.py` 抓取并清洗为「一条一段」的 Markdown，文件首段记录来源。

标注用例集（`labels.py`）：36 条命中问题（事实型、文号/专名型、跨语种、PDF 分页定位）+ 5 条语料外负面问题。`tests/integration/test_effectiveness.py` 跑真实链路验证：检索命中预期条款、hit@1 / hit@5 指标、无答案判定、PDF 页码与 DOCX 段落 locator、MD 字符偏移与原文一致、知识库范围隔离。

当前基线（HashEmbedder）：36/36 top10 命中、hit@5 97%、hit@1 89%、负面 5/5 no_evidence。阈值 `KS_SIMILARITY_FLOOR=0.06` 按该语料标定——语料外问题关键词路最高 0.059、真实命中最低 0.058，两者接近重叠，靠语义路兜底；**换真实向量模型后问题集与阈值必须重新标定**，评测结果不代表真实模型的语义检索质量。

## 模型

未配置端点时使用本地确定性实现：`HashEmbedder`（字符二元组哈希，1024 维）与 `StubGenerator`（复述首条证据）。链路可跑通，**但不代表检索与生成质量**。接入真实模型只需设置：

```bash
export KS_EMBEDDING_BASE_URL=http://... KS_EMBEDDING_MODEL=bge-m3
export KS_GENERATOR_BASE_URL=http://... KS_GENERATOR_MODEL=qwen
```

换向量模型后必须重建索引，并重新标定 `KS_SEMANTIC_MAX_DISTANCE` 与 `KS_SIMILARITY_FLOOR`。
