# 后端服务：知识检索与智能问答助手

实现规格见 [../docs/design.md](../docs/design.md)，跑起来与排障见 [../docs/runbook.md](../docs/runbook.md)。

## 快速开始（不需要安装 PostgreSQL）

**浏览器界面已接入**：根目录启动器在资料就绪后打开“知页”工作台。
在启动终端输入 `/token`，把本次开发令牌粘贴到页面即可操作知识库、上传、检索、问答与引用。
离线体验：`start.cmd -Mode Demo`；不打开浏览器：加 `-NoBrowser`。
这是临时开发环境，退出后资料清理；详见 [工作台操作与验收](../docs/runbook.md#13-浏览器知识工作台)。

首次安装或交付验收请先看 [固定环境与完整检查](../docs/runbook.md#10-固定环境与完整交付检查)。
完整检查入口：`python -u backend/scripts/check.py`，或在 backend 目录执行 `make check`；
它验证固定版本并运行隔离回归，不调用真实模型。

失败任务恢复和主动重建使用 `POST /api/v1/documents/{document_id}/reindex`，
必传上传回执的 `version_id` 与最新 `expected_generation`。操作步骤见
[人工恢复与重建](../docs/runbook.md#11-人工恢复与同模型重建)；不通过重复上传触发恢复。

**想先看实际运行结果**：在仓库根目录执行以下命令，无需手动分别启动服务：

Windows 下直接双击项目根目录的 `start.cmd`，默认进入**真实模型终端问答**。
它读取 `backend/.env` 中的模型配置，启动临时数据库、API、Worker 并导入自制资料；
出现“请输入问题”后直接输入，即可看到回答、引用原文和定位。输入 `/quit` 退出。
默认自动选择空闲端口，也可指定 `start.cmd -Port 8011`；只检查配置使用 `start.cmd -CheckOnly`。
真实模式要求向量及生成端点均已配置，且向量为 1024 维；失败明确报错，不回退到模型桩。
密钥不输出，问答正文不写入报告或服务日志。此入口不使用现有业务数据库，临时资料退出后清理。

不调用真实模型时使用 `start.cmd -Mode Demo`，先跑 10 步验收，再进入本地模型桩终端问答。
以下直接运行 Python 的方式用于单次离线链路演示：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/demo_local.py
```

终端会显示 10 步 HTTP 验收与任务状态；答案、引用及版本更新前后对比见
`backend/.data/demo/latest.md`。加 `--serve --port 8010` 可在验收后打开 Swagger 手动操作。
详细步骤与开发鉴权见 [运行手册 §9](../docs/runbook.md#9-可观察演示与-http-端到端验收)。
此演示使用自制资料和本地确定性模型桩，不代表真实大模型效果。

依赖里自带 `embedded-postgres`（PostgreSQL 18.6 + pgvector 0.8.6），开发时用它起一个本地库。

```bash
cd backend
pip install -r requirements-dev.lock

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

推荐从仓库根目录运行完整隔离回归，实时显示测试名称和 HTTP 步骤：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/test_isolated.py
```

它显式启用真实 HTTP + 独立 Worker 的 E2E，并创建专用测试数据库；普通 `pytest` 不设置
E2E 环境时会跳过该组测试，不等同于完整验收。

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

标注用例集（`labels.py`）：36 条字面命中问题（事实型、文号/专名型、跨语种、PDF 分页定位）+ 4 条语义改写问题（与原文几乎无字面重叠，仅真实向量下参与断言）+ 6 条领域外负面问题 + 1 条主题相邻硬负面（与命中分布重叠，仅记录，属无重排时的已知局限）。`tests/integration/test_effectiveness.py` 跑真实链路验证：检索命中预期条款、hit@1 / hit@5 指标、无答案判定、PDF 页码与 DOCX 段落 locator、MD 字符偏移与原文一致、知识库范围隔离。

**两套基线**（默认哈希向量是回归门槛；真实向量需 Ollama bge-m3 在跑并设 `NO_PROXY=127.0.0.1,localhost`）：

| 指标 | HashEmbedder（默认，`pytest -q`） | bge-m3 真实向量（`KS_TEST_REAL_EMBEDDER=1`） |
| --- | --- | --- |
| 命中用例 | 36 条字面 | 40 条（36 字面 + 4 语义改写） |
| top10 全命中 / hit@5 / hit@1 | 全中 / 97% / 89% | 全中 / 100% / 85%* |
| 语义改写（无字面重叠） | 0/4（跳过，哈希必失败） | **4/4** |
| 领域外负面拒答 | 6/6 | 6/6 |

\* hit@1 分母含 4 条高难改写用例，不可与哈希行直接比。

阈值按当前生效向量标定（`scripts/calibrate.py`）：哈希基线 `KS_SIMILARITY_FLOOR=0.06` / `KS_SEMANTIC_MAX_DISTANCE=0.85`（config 默认，测试夹具在非真实模式下钉住）；bge-m3 实测 `0.06` / `0.42`（真实命中距离 max 0.393、领域外负面 min 0.431，写入 `.env`）。换向量模型后必须重建索引并重跑标定。

## 格式覆盖与规模基线

`tests/integration/test_format_variety.py` 覆盖：DOCX 表格（单元格按行展平参与索引，结构化解析不在首期）、PDF 表格（文本流提取，顺序可能交错）、加密 PDF（确定性失败 `PASSWORD_PROTECTED`，不重试）、空白 PDF（`EMPTY_CONTENT`）、白名单外格式（xlsx/csv/pptx/jpg 一律 415）。

`scripts/scale_test.py` 为合成文档压测（实测值，本地嵌入式 PostgreSQL，不外推）：

| 规模 | 向量 | 入库吞吐 | 检索延迟 p50 / p95 |
| --- | --- | --- | --- |
| 300 份 / 3900 块 | HashEmbedder | 597 块/s | 37ms / 44ms |
| 1000 份 / 13000 块 | HashEmbedder | 608 块/s | 112ms / 129ms |
| 40 份 / 520 块 | bge-m3（Ollama，CPU） | 5 块/s | 2296ms / 2404ms（查询含 CPU 向量化） |

SQL 与排序层随规模增长平稳；真实向量的瓶颈在本机 CPU 嵌入算力（入库与查询向量化），换 GPU 或远程 API 端点可消除。

## 模型

未配置端点时使用本地确定性实现：`HashEmbedder`（字符二元组哈希，1024 维）与 `StubGenerator`（复述首条证据）。链路可跑通，**但不代表检索与生成质量**。

上述回退只适用于基础适配器和显式 Demo/测试模式；根目录启动器默认 Real 模式会先验证配置，禁止缺少真实端点时静默回退。

配置写在 `backend/.env`（不入库），两条路线：

| 路线 | 生成侧 | 向量侧 | 说明 |
| --- | --- | --- | --- |
| 在线 API（当前） | DeepSeek `deepseek-chat` | SiliconFlow `BAAI/bge-m3`（1024 维，免费额度） | 只需填 `KS_GENERATOR_API_KEY` 与 `KS_EMBEDDING_API_KEY` |
| 离线预留 | Ollama（如 `qwen2.5:7b`） | Ollama `bge-m3` | 本机已装 Ollama；`ollama pull bge-m3` 后切换，无需 key |

注意：向量模型必须输出 1024 维（表结构 `vector(1024)`，禁止混维度）；本机走 Ollama 前设 `NO_PROXY=127.0.0.1,localhost`，否则本地请求被系统代理截走。

换向量模型后必须重建索引（新上传/新构建），并跑 `python scripts/calibrate.py` 重新标定 `KS_SIMILARITY_FLOOR` 与 `KS_SEMANTIC_MAX_DISTANCE`。
