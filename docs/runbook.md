# 运行手册

_二创项目 · v0.3 · 2026-10-23_

> 启动、日常操作与故障处置。命令入口为约定名称，M1 落地后以其为准；入口变更须回写本文，否则视为失效。

---

## 1. 前置检查

| 项 | 要求 | 检查 |
| --- | --- | --- |
| Python | 3.11+；与 QwenPaw 同环境时须 3.11–3.13 | `python -V` |
| PostgreSQL | 已装 `vector` 与 `pg_trgm` | `SELECT extname FROM pg_extension;` |
| 文件目录 | 一个可写目录作为对象存储根 | 启动时自动创建 |
| 模型端点 | 生成、向量各一个，地址由环境变量注入 | `curl` 探活 |

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

## 2. 启动

开发环境不需要另装数据库：`python scripts/dev_db.py` 会用依赖里的 `embedded-postgres`（PostgreSQL 18.6 + pgvector）起一个本地实例，连接串写入 `.data/db_url`。

```bash
cd backend
pip install -e ".[dev]"

# 可选：本地数据库（前台运行，另开终端）
python scripts/dev_db.py

export KS_DATABASE_URL=$(cat .data/db_url)     # 已有 PostgreSQL 时直接指向自己的库
export KS_STORAGE_ROOT="./.data/objects"
export KS_EMBEDDING_BASE_URL="..."  KS_EMBEDDING_MODEL="bge-m3"
export KS_GENERATOR_BASE_URL="..."

alembic upgrade head                                          # 1) 迁移
uvicorn knowledge.entrypoints.api:app --port 8010             # 2) API
python -m knowledge.entrypoints.worker                        # 3) Worker（另一终端）
```

自检：`GET /api/v1/knowledge-bases` 能返回空列表，说明数据库连接、迁移与令牌校验都正常；跑 `python scripts/demo_flow.py` 可验证完整链路。

**顺序**：迁移先于 API 与 Worker；API 与 Worker 彼此无先后。Worker 未启动时上传停在 `queued`，这是正常状态而非故障。

## 3. 日常操作

```bash
# 建库
curl -s -X POST localhost:8010/api/v1/knowledge-bases \
  -H "Authorization: Bearer $KS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"制度库"}'

# 上传（返回 job_id，不等待解析）
curl -s -X POST localhost:8010/api/v1/knowledge-bases/$KB/documents \
  -H "Authorization: Bearer $KS_TOKEN" -F "file=@./testdata/engineering/meeting-room.md"

# 查任务
curl -s localhost:8010/api/v1/jobs/$JOB -H "Authorization: Bearer $KS_TOKEN"

# 检索调试：观察两路排名与融合结果
curl -s -X POST localhost:8010/api/v1/search \
  -H "Authorization: Bearer $KS_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"query\":\"会议室使用后要做什么\",\"knowledge_base_ids\":[\"$KB\"],\"mode\":\"hybrid\"}"
```

任务 `failed` 时先读 `last_error.code`：`UNSUPPORTED_FORMAT` / `PASSWORD_PROTECTED` / `EMPTY_CONTENT` 属确定性错误，不重试。

## 4. 故障处置

| 症状 | 判断 | 处置 |
| --- | --- | --- |
| 长时间 `queued` | Worker 未启动或已退出 | 启动 Worker；无需重新上传，任务已在表中 |
| 长期 `running` 且 `heartbeat_at` 不再更新 | 执行进程已死，租约 120 秒后过期 | 存活 Worker 每轮领取前回收；若全部停止则启动 Worker；不要手工改 `state` |
| 反复 `failed` 且 `attempt` 已达上限 | 内容本身有问题 | 修文件后重新上传；不要提高重试次数掩盖问题 |
| 检索 0 命中但文档 active | `active_build_id` 为空或 build 非 ready | 查 §5 指针一致性 SQL；处理完成前属预期行为 |
| 向量维度错误 | 换了向量模型但未重建 | 新建构建并重建；不得在同一索引混用不同维度 |
| 迁移卡住 | 另一进程持锁 | 查 `pg_locks`，确认无其他迁移进程后重试 |
| 磁盘持续增长 | 孤儿对象或清理积压 | Worker 启动时自动回收超过 1 小时且无引用的对象（`orphans_reclaimed` 日志）；反复增长说明上传事务持续失败或 cleanup 积压，查 `jobs` 表与 `upload` 接口错误 |
| 删除后仍能检索到 | 逻辑删除未提交，或代码缺陷 | 查文档 `status` 与 `active_build_id`；本服务不缓存检索结果 |

## 5. 诊断 SQL

```sql
-- 任务分布与最老排队时间
SELECT state, stage, count(*), min(available_at)
FROM jobs GROUP BY state, stage ORDER BY state;

-- 租约已过期但仍在 running（可被接管）
SELECT id, kind, resource_id, attempt, lease_expires_at, heartbeat_at
FROM jobs WHERE state = 'running' AND lease_expires_at < now();

-- 指针一致性：active_build 必须存在且 ready
SELECT d.id, d.status, d.active_build_id, b.state
FROM documents d LEFT JOIN index_builds b ON b.id = d.active_build_id
WHERE d.status = 'active'
  AND (d.active_build_id IS NULL OR b.state <> 'ready');

-- 重复 chunk（应为 0 行）
SELECT build_id, ordinal, count(*) FROM chunks
GROUP BY build_id, ordinal HAVING count(*) > 1;

-- 分块数与向量维度
SELECT build_id, count(*), min(vector_dims(embedding))
FROM chunks GROUP BY build_id;

-- 清理积压
SELECT count(*) FROM jobs WHERE kind = 'cleanup' AND state IN ('queued','running');
```

## 6. 变更纪律

- **迁移只增不改**：已执行的迁移不修改，修正用新迁移。
- **Worker 先停后改结构**：涉及 `jobs` / `chunks` 的迁移，先停 Worker 再迁移。
- **重建索引走新构建**：不直接改 `chunks`，不删 `ready` 构建的数据。
- **不手工改状态字段**：`state`、`generation`、发布指针只由业务流程写，手工修改会破坏发布栅栏与任务过期判定。

## 7. 日志与追踪

日志为单行 JSON（`ts` / `level` / `logger` / `event` + 业务字段）。请求日志含 `request_id`；任务相关再加 `job_id`、`build_id`。关键事件名：`http_request`、`job_claimed`（含排队时长）、`stage_done`（各阶段耗时）、`job_retry_scheduled`、`job_failed`、`lease_recovered`、`build_published`、`qa_no_evidence`、`qa_answered`、`citation_validation_failed`。

1. 用 `request_id` 定位请求；
2. 涉及处理时用 `job_id` 查任务时间线（领取、阶段、重试、失败原因）；
3. 涉及检索或引用时用 `build_id` 确认当时生效的构建，不要用当前状态解释历史行为。

日志不含问题正文、分块内容、答案与令牌；需要正文复现时用显式授权的临时调试模式，用完即关。

## 8. 审计修复版本升级与验证

### 任务目标迁移 0002

1. 停止上传入口，等待 ingest/reindex 任务排空；历史卡住任务需先调查并经授权处置，不直接猜测其目标构建。
2. 停止所有 Worker，备份数据库，执行 `alembic upgrade head`。
3. 迁移发现未完成 ingest/reindex 时明确失败并回滚；不要绕过检查。已结束任务允许目标字段为空。
4. 启动新版本 Worker 与 API；确认新任务含 build_id/generation，重试后 job ID 不变而 build ID 改变。

旧失败构建不会再被覆盖；其残留数据随文档清理，不作为检索来源。
切换向量模型前安排离线索引重建；不兼容时恢复原模型配置，或显式使用 keyword，不得把 503 当作无答案。

### 解析限制

统一 Settings 提供 `KS_PARSE_TIMEOUT_SECONDS`、`KS_PARSE_CPU_SECONDS`、`KS_PARSE_MEMORY_BYTES`、
`KS_PARSE_OUTPUT_BYTES`、`KS_DOCX_MAX_ENTRIES`、`KS_DOCX_MAX_UNCOMPRESSED_BYTES`。
默认值见 `backend/knowledge/config.py`，是资源保护配置，不是性能承诺。超限归为 PARSE_FAILED，
结束解析子进程，不无限续租重试。需要更严格的内存硬限额时在部署层设置进程/容器配额。

### 隔离回归

从仓库根目录运行 `backend/.venv/Scripts/python.exe backend/scripts/test_isolated.py`（Windows）。
脚本创建独立临时 PostgreSQL 和迁移测试库，仅使用本地模型桩；不连接现有业务数据库，结束后停止服务器。
Windows 非 ASCII 安装路径会将测试数据库运行组件复制到独立临时目录；临时目录使用 `ks_audit_test_` / `ks_audit_pg_` 前缀，确认进程退出后可清理。
测试过程不会读取项目 backend/.env；禁止将现有集成测试夹具指向业务库，因为夹具会重建表。

## 9. 可观察演示与 HTTP 端到端验收

### 一条命令看完整流程

在仓库根目录执行（Windows PowerShell）：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/demo_local.py
```

入口自动创建临时数据库、执行迁移、启动独立 API 与 Worker，再通过真实 HTTP 执行 10 步验收。
它只使用仓库自制资料、HashEmbedder 和 StubGenerator，不读取项目 `.env`，不调用外部模型，
不重置现有数据库。终端逐步输出启动进度、进程 PID、任务 state/stage/attempt 与校验结果，
每条输出立即刷新。服务日志只包含运行元数据，不把答案、问题或令牌写入日志。

演示结束后自动停止进程与临时数据库，结果保存在 `backend/.data/demo/latest.md`：
包括实际 HTTP 返回的版本一/版本二答案、引用和定位，明确标注为确定性桩的链路演示。
答案不是预填文本；该演示不代替真实模型效果评测。每次报告含生成时间，避免与历史结果混淆。

API、Worker 与迁移日志保存在 `backend/.data/e2e/<本次运行编号>/`，失败时按终端路径查看。

### 保持服务运行，自己调用

根目录 `start.cmd` 默认进入真实模型终端问答：

```powershell
.\start.cmd
# 显式指定端口；默认 0 自动选择，避免误连旧演示实例。
.\start.cmd -Port 8011
# 检查环境、启动依赖并自检模型，不进入问答。
.\start.cmd -CheckOnly
# 本机缺少向量模型时自动拉取（约 1.2 GB）。
.\start.cmd -PullMissing
# 完全离线：10 步验收后进入模型桩终端问答。
.\start.cmd -Mode Demo
```

`start.cmd` 优先使用 PowerShell 7，没有时使用 Windows PowerShell 5.1，统一调用根目录
`start.ps1`；也可直接执行 `.\start.ps1 -Port 8011`。它检查项目虚拟环境、依赖与端口，
失败时明确提示，不自动下载依赖、不停止其他进程。模式为 `Real` / `Demo`，均使用隔离临时数据库与资料卷，
不会采用 `.env` 中的数据库地址、存储路径或身份密钥。

Real 模式从 `backend/.env` 和环境变量读取模型字段：`KS_EMBEDDING_BASE_URL` / `MODEL` / `API_KEY` / `DIM`，
`KS_GENERATOR_BASE_URL` / `MODEL` / `API_KEY`，以及相关超时、检索阈值和任务配置。
维度必须为 1024；端点、模型名缺失时拒绝启动，不回退 HashEmbedder / StubGenerator。

### 依赖自动启动

Real 模式在启动服务前先处理依赖：端点为本机（`localhost` / `127.0.0.1`）且端口未监听时，
自动拉起 `ollama serve`（日志 `backend/.data/deps/ollama.log`），并确认配置的向量模型已安装；
缺模型时默认提示 `ollama pull <model>`，加 `-PullMissing` 改为自动拉取。

随后真实调用一次 `/embeddings` 校验实际维度与配置一致，再用 1 个 token 调用 `/chat/completions`
校验生成端点、密钥与余额。任一环节失败即停止启动并输出具体原因（未启动 / 鉴权 / 余额 / 模型名 /
维度不符），不回退桩模型。数据库、API 与 Worker 仍由运行环境按每次运行现起。

出口策略：本机端点直连，不经过系统代理；远程端点沿用环境代理配置。
可选字段留空会忽略。首次仅导入仓库自制会议室资料，准备好后在当前终端输入问题，
显示答案、服务端引用原文、文件名和字符/页码定位；等待期间持续显示耗时。
非敏感示例问题：“会议室使用后需要做什么？”“预约需要提前多久？”

真实模式的问题和证据会发送到已配置模型服务，可能产生费用；仅用经授权的资料和问题。
启动器不打印 API 密钥；终端问答不传 conversation_id，不保存问答存档，也不写 Demo 报告。
输入 `/quit` / `/exit`、EOF 或 Ctrl+C 退出并清理本次进程。此控制台按当前问题独立问答，不实现多轮指代。

真实模式的单次复验命令（建议仅用于自制示例，真实问题不要写入 shell 历史）：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/ask_local.py --question "会议室使用后需要做什么？"
```

Demo 模式直接运行 Python 的等价命令：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/demo_local.py --serve --port 8010
```

Demo 正常验收后，程序额外建立一个供手动体验的知识库，并输出其 ID，然后进入终端问答。打开本机
`http://127.0.0.1:8010/docs`；端口占用时换一个端口，或者省略 `--port` 自动选空闲端口。
服务仅监听 127.0.0.1；终端中的实际地址以本次输出为准。输入 `/quit` 或按 Ctrl+C 结束体验。临时资料不作为持久知识库保存。

演示环境使用默认的本地开发令牌桩，主体为 `demo-owner`。在另一个终端、仓库根目录执行：

```powershell
$env:KS_TOKEN_SECRET = "dev-only-secret"
.\backend\.venv\Scripts\python.exe -m knowledge.security issue --subject demo-owner
```

这是显式签发开发令牌的命令，不是服务日志。令牌只用于当前本机演示，不提交、不写入报告。
Swagger 中每个需要鉴权的操作有 `authorization` 请求头参数，填写 `Bearer <开发令牌>`。
先调用 GET `/api/v1/knowledge-bases` 查看手动体验库，再用其 ID 调用 POST `/api/v1/qa`。
建议问题与样本答案见演示报告。此入口不接入真实身份或真实资料。

### 运行有实时进度的完整回归

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/test_isolated.py
```

默认打印测试名称、通过/失败与 E2E 步骤。入口复用临时数据库管理，为服务层集成、迁移和
HTTP E2E 使用不同数据库；显式启用进程测试，不能用跳过 E2E 的普通 pytest 结果代替完整验收。
完整验收禁止跳过或排除用例；传入 `-k` 排除测试会以非零退出，不能当作完整验收通过。
完整回归中的模型接口故障由本机 HTTP 桩制造，
Worker 崩溃测试会实际终止处于 embedding 阶段的 Worker，再在租约过期前重启，
等待其自动回收并以新 build 完成，不手动修改数据库租约。

## 10. 固定环境与完整交付检查

开发验收基线固定为根目录 `.python-version` 中的 Python 版本；依赖版本统一维护在
`backend/requirements-dev.lock`。这是版本快照，不含制品哈希，不表示供应链完整性验证。
`pyproject.toml` 保留库依赖范围；安装时使用锁文件，修改依赖后必须同步锁文件并重跑验收。

新环境安装（在项目根目录执行，需允许从软件包源下载依赖）：

```powershell
py -3.12 -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.lock
.\backend\.venv\Scripts\python.exe -u backend/scripts/check.py
```

创建环境前确认解释器的补丁版本也与 `.python-version` 一致；`py -3.12` 本身不保证补丁版本。
脚本从源码目录运行，不要求 editable 安装。Linux 使用 `.venv/bin/python` 对应命令。
现有环境不要盲目重装或删除；完整检查会列出缺失、漂移的依赖并退出，不自行安装。

统一入口按顺序显示：版本预检 → lint → 业务类型检查 → 脚本类型检查 → 独立数据库完整回归。
任一步失败立即停止；数据库迁移、HTTP 生命周期、Worker 重启均在回归内。
跳过、xfail/xpass、排除用例或未收集到测试均不能让完整验收显示成功。
普通 `pytest` 仍可用于快速开发，但不能替代此入口。

PDF 中文测试默认使用 Windows 黑体。其他环境须提供本地中文字体，例如：

```bash
export KS_TEST_FONT_PATH=/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc
python -u backend/scripts/check.py
```

CI 配置位于 `.github/workflows/backend.yml`，使用 Ubuntu、固定 Python、同一锁文件及检查入口，
额外安装文泉驿中文测试字体。CI 不注入模型密钥或业务数据库配置，不上传问答/服务日志。
本地 Windows 验证与远端 Ubuntu 流水线结果须分别记录，不能把配置文件存在当作云端运行通过。

## 11. 人工恢复与同模型重建

接口契约只在 design §7.1 定义。以下是调用顺序，不增加重试/回滚语义。

1. 保留上传/新版本上传回执中的 `document_id`、`version_id`、`job_id`。
2. 查询 GET `/api/v1/jobs/{job_id}`。queued/running 时继续等待，不发重建请求。
3. failed 后先排除原因：例如模型恢复可用、Worker 恢复原模型配置。加密、空内容或不可解析文件
   不是重建可以修好的问题，应按业务要求上传修正后的新版本。
4. GET `/api/v1/documents/{document_id}` 读取最新 `generation`；保留上传回执中的目标版本。
5. 在 Swagger 中携带本人的开发令牌，调用 POST `/api/v1/documents/{document_id}/reindex`：

```json
{"version_id": "目标上传回执中的 UUID", "expected_generation": 2}
```

这里的 2 仅示例，必须替换为实际代次。首次上传失败时 `current_version_id` 可以为空；
新版本失败时它仍指向旧发布版本。不要把 current_version_id 误作最新上传版本。

6. 202 响应保存新的 `job_id` / `build_id` / `generation`，按原来的任务查询接口轮询。
   新任务重新计数，历史失败 job 不改成成功。重建期间仍可检索旧发布内容，成功后引用绑定新 build。
7. 若遇 409 VERSION_CONFLICT，刷新详情并查询最新任务，不盲目重发。旧 generation 的重复请求不会被当作成功。
   如果 Worker 不是原模型/维度，恢复原配置后再处理；此接口不执行跨模型迁移或历史版本回滚。
8. 删除中、删除后或非本人资源返回 404，不需要等物理清理完成。

接口同时可用于已成功最新版本的主动重建，不复制文件或增加版本。
沿用原构建的分段与向量配置；修改 settings 中分段参数不会借此接口改变原构建配置。
启动方式仍按 §9：`start.cmd -Mode Demo` 可离线在 Swagger 体验，不调用真实模型。
开发令牌签发方式见 §9；不要将令牌、正文或真实资料贴入文档或提交记录。

可观察自动验收仍使用 §10 的统一入口。终端显示 `[reindex 1/5]` 至 `[reindex 5/5]`；
人工恢复验收显示 `[recovery 1/4]` 至 `[recovery 4/4]`，在本机 HTTP 模型桩返回 503 后，
让独立 Worker 按真实退避耗尽三次自动尝试，再经公开重建接口恢复，不手工修改任务状态。

## 12. 固定问题集与效果评测

数据集只取 `backend/testdata/evaluation/` 和既有自制会议室 v1/v2 文件，
不读取旧语料中的抓取文件、甲方数据或用户任意目录。问题集包含 12 个固定用例：
精确事实、语义改写、跨来源信息、文号、库外、同主题无答案、版本更新、文档内指令干扰。
`cases.json` 是初始开发标注，需业务方复核，不是已确认的业务验收标准。

在项目根目录执行离线评测：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/evaluate_local.py
```

显式获得一次真实模型调用授权后，才执行：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/evaluate_local.py --real --confirm-real
```

真实模式读取既有模型白名单配置，仍使用临时独立数据库、API、Worker 和固定自制资料；
不接受自定义数据路径或问题，缺少确认参数时不读取真实配置、不调用模型。
固定每题三种检索及一次问答，共 48 次检索/问答 API 请求，上限 60，不自动重跑。
上传、轮询、引用读取是额外本地 API 请求；上传会产生向量调用，后台故障按原有任务契约重试。
token 用量与费用因现有 API 未提供而记为 null，不能视作免费，也不能把本地 API 请求数当供应商精确计费次数。

终端逐题显示 ID、分类、状态和自动发现，不打印问题/答案。指标报告写入忽略目录
`backend/.data/evaluation/<本次编号>/report.json`，保存语料指纹、模型配置名、阈值、逐题指标与耗时，
不保存问题、答案、原文、令牌、端点或资源存储路径；模型 ID 不等于已固定的模型权重版本。

| 指标 | 表示什么 | 不能据此认定什么 |
| --- | --- | --- |
| gold_fact_recall_at_5 / first_relevant_rank / MRR | 正确文档及当前版本的支持片段是否进入 Top5、首次位置 | 生成答案正确 |
| fact_lexical_coverage | 标注事实的匹配词是否出现在答案 | 否定、推断、额外事实等语义正确 |
| gold_support_coverage | 标注支持片段是否在实际引用的正确版本原文中 | 答案每句话都得到原文支持 |
| status_matches_label | answered/no_evidence 状态是否符合可回答标注 | 模型没有编造，或正文拒答与状态完全一致 |
| forbidden_text_detected | 命中版本过期词或注入探针词，需人工检查 | 单凭命中就能判定恶意服从或事实错误 |

引用另外经 GET chunks 验证可访问性、四个资源 ID、定位与摘录。注入用例先检查探针是否确实进入混合检索证据，
未暴露时记录 instruction_probe_not_exposed，不宣称抗注入验证成功。
不可答题被标为 answered 记录 unanswerable_not_no_evidence；这是状态契约不符，不直接等同于事实幻觉。
上游错误单独记录 qa_error_not_refusal，不能当作正确拒答。统计显示错误数，检索错误按零计入可回答题召回分母。

退出码 0：没有自动发现；2：执行完成但发现待处理项；1：配置、启动或执行未完成；130：人工中止。
即便退出 0，quality_accepted 仍为 false，语义支持与业务标注均待人工复核。
评测失败不能通过改标注、阈值或跳过用例掩盖；生产拒答/部分段落无证据行为沿用 design §6.2，变更需评审。
效果命令与快速回归分开；统一检查仅自动运行离线评测工具与自制 HTTP 夹具测试，不调用真实模型。

## 13. 浏览器知识工作台

### 启动与连接

根目录双击 start.cmd，默认 Real 模式；自制资料处理完成后会打开本次服务的 `/workbench/` 页面。
离线体验用 `start.cmd -Mode Demo`；避免自动打开浏览器用 `-NoBrowser`。
浏览器与终端共用本次临时 API/Worker，不增加前端服务，不需要 npm install 或构建步骤。

1. 启动器把本次令牌放在地址片段里打开页面，**页面自动连接，无需手工填写**；
   片段不进入请求行、不写服务端日志，页面读取后立即从地址栏清除。
2. 令牌密钥每次运行随机生成，浏览器历史里残留的旧令牌对下一次运行无效。
   若手工打开 `/workbench/`（不带片段），页面仍会弹出连接窗口，此时在终端输入 `/token` 复制粘贴。
3. 创建/打开知识库，上传自制文件并等待状态“已就绪”；不是按假百分比展示进度。
4. 检索测试支持三种方式，显示接口片段、来源、实际分数；智能问答点击引用可重新读取授权原文。
5. 页面支持新版本上传、同模型重建、文档与知识库删除。删除有确认框；重建沿用 §11 的既有契约。
6. 终端 `/quit` 或 Ctrl+C 结束本次临时服务，临时数据库和原件被清理，**不是持久个人资料部署**。

无证据、模型故障、引用失败分别展示；同主题无答案的已知 502 不会包装成成功或正常拒答。
问答每次独立，没有多轮上下文。引用仅为文本分块，不提供原文件保真预览。
真实模式上传与提问可能向配置的模型发送内容；开发体验只用自制资料。

### 身份、刷新与限制

令牌、上传回执和问答仅在页面内存中；刷新需重新连接。点击左下身份按钮或右上“我”可断开并清空内容。
不使用 localStorage/sessionStorage 保存令牌、问答，不把令牌放进 URL，也不传模型密钥到页面。
旧身份未结束的响应在断开/切换后丢弃。

失败的新版本可能没有 current_version_id。若刷新丢失上传回执，页面不能可靠推断恢复目标，
因此禁用对应重建操作；此时按 §11 使用保留的回执调用接口，不以旧发布版本替代。
页面保留明确的上传跳过/失败信息；自动刷新失败会提示，不静默假装处理完成。

### 检查与浏览器验收

```powershell
node --check frontend/app.mjs
node --test frontend/core.test.mjs
.\backend\.venv\Scripts\python.exe -u backend/scripts/check.py
.\backend\.venv\Scripts\python.exe -u backend/scripts/browser_smoke.py
```

界面运行本身无需 Node；测试需已有 Node.js 22+。浏览器验收另需本机 Edge/Chrome，缺少即报错，
不自动安装、不跳过后宣称通过。使用独立浏览器配置，不读取个人浏览器状态。
截图及测试配置保存在忽略目录 `backend/.data/workbench/<编号>/`，不是生产数据或提交产物。

浏览器验证连接、实际建库/上传、任务就绪、回答/引用、正常无证据、检索、重建/更新/删除、
HTML 文本安全、窄屏和刷新清理；HTTP 502 的界面分支采用显式浏览器故障注入，不冒充真实模型结果。
其中「分块结果」一步会打开文档行的分块视图，核对覆盖条色块数与明细卡片数一致、首块编号为 `#1`，
截图存为 `chunks.png`。缺少字符偏移时视图退化为等宽展示并在提示中说明，不伪造覆盖比例。
CI 增加 Node 原生协议测试；本机浏览器通过不等于远端 CI 浏览器验收通过。

静态目录由源码布局定位；独立安装后端 wheel 不会自动打包根目录 frontend。
正式交付需按主系统部署方式提供静态资产和正式身份；没有完成这一步就不能宣称生产前端已交付。
## 14. DeepDoc 可选服务（2026-10-27）

契约见 design §2.1，决策见 decisions 0015。主后端默认仍走旧解析，不会因缺少模型静默降级。

### 14.1 环境与启动

需要支持 Linux 容器的 Docker Desktop + Compose、可拉取镜像的软件网络及足够的磁盘/内存。
复用的 RAGFlow 镜像包含较多依赖，首次构建不是轻量下载；不自动安装 Docker 或改系统配置。

本机按用户要求将 Docker 程序安装到 `D:\Docker\Desktop`，WSL 数据盘指定为 `D:\Docker\wsl`，
安装包与临时目录为 `D:\Docker\Downloads`、`D:\Docker\Temp`，不把镜像/容器大数据放到 C 盘。
这不等于 Windows 零系统盘写入：注册信息及少量用户配置由操作系统管理。
根目录 `install-docker.ps1` 只运行 D 盘指定位置、数字签名有效的官方安装包，
使用官方 `--installation-dir` / `--wsl-default-data-root` 参数；需要管理员确认，不自动重启。
安装成功与引擎就绪分开验证，数据盘实际位置确认前不拉取业务镜像；不会修改已有 Ubuntu 发行版。

本机默认 C 盘 Docker 配置目录写入失败，目录带 Encrypted 属性；复制及改名也被拒绝，
未更改其权限、加密设置或原文件，未生成备份或目录链接。实际采用根目录 `start-docker.ps1`：
仅给 Docker 子进程指定 `D:\Docker\UserData\Roaming`、`D:\Docker\UserData\Local` 和 D 盘临时目录，
启动后恢复调用终端的环境变量。不要从普通快捷方式启动来绕过该入口；它可能再次使用原 C 盘配置。
2026-10-27 已验证 Linux 引擎及 Compose 可用，实际业务数据盘为
`D:\Docker\wsl\disk\docker_data.vhdx`。该结论不等于 DeepDoc 镜像或解析效果已验收。

根目录 PowerShell：

```powershell
.\start-docker.ps1
.\start-deepdoc.ps1 -CheckOnly
.\start-deepdoc.ps1 -LaunchWorkbench
```

脚本按步骤实时输出检查、构建和启动信息；生成的共享令牌不打印、不写文件。
只启动边车可省略 `-LaunchWorkbench`，再在**同一个 PowerShell 会话**运行 `.\start.cmd`。
工作台仍是原真实模型模式，问题/证据会送配置的生成与向量服务；DeepDoc 本身不调用外部模型。
演示/隔离测试仍保持旧解析，不继承真实解析端点。

主系统自行部署 API/Worker 时，两者显式配置相同的 `KS_DOCUMENT_PARSER_PROFILE`、
`KS_DEEPDOC_URL`、`KS_DEEPDOC_TOKEN`；令牌与边车 `DEEPDOC_TOKEN` 相同（至少32字符）。
不得把令牌提交进仓库。`KS_DEEPDOC_TIMEOUT_SECONDS` 是网络等待上限，不是处理时延目标。
主服务连边车仅支持回环 HTTP；跨机/TLS/外部解析 SaaS 不属于本次契约。

`deploy/deepdoc/compose.yaml` 可独立使用；固定 release 镜像后可通过 `RAGFLOW_IMAGE`
指定同一 release 的镜像 digest。构建检查上游 VERSION，边车 HTTP 依赖锁定且不改上游环境。
容器运行期禁外连并设置 HuggingFace 离线模式，模型缺失时失败，不在处理资料时偷偷下载。
这里的禁外连指 `deepdoc` 解析容器。`gateway` 是复用同镜像 Nginx 的固定入口，连接入口和内部网络，
仅发布宿主回环端口；不加载模型，不接收动态目标，不打印请求或正文。部署依据见 decisions 0016。
不要把解析容器额外接到普通桥接网络；本机仅内部网络直接发布端口不可达，必须保留入口代理。

Docker Hub 令牌请求超时时，可显式使用同版本的上游国内镜像，不改系统 DNS 或使用未知加速源：

```powershell
$env:RAGFLOW_IMAGE = 'swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow:v0.22.0@sha256:3e7f2b8a796f55bf7fde363638086f32f7017e9d01bee753f827b50725ba8d7a'
.\start-deepdoc.ps1
```

地址依据：[上游 v0.22.0 配置](https://github.com/infiniflow/ragflow/blob/v0.22.0/docker/.env)。
此 digest 是本次解析到的镜像内容标识，不冒充模型权重的独立指纹验收。

### 14.2 查看与停止

```powershell
Invoke-RestMethod http://127.0.0.1:9381/health
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml logs -f
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml down
```

这些命令需要同一个 `DEEPDOC_TOKEN` 环境变量以解析 Compose 配置；日志只输出请求 ID 和耗时，
不输出 OCR 文本/文件名。`health.models_verified=false` 是设计上的提示，不是推理就绪证明。
工作台退出**不停止**边车；停止后已发布内容仍可检索，但新的 DeepDoc 任务会失败。
重启 PowerShell 后令牌不会保留；重新运行脚本会生成新令牌并重建相同项目的容器，
同时需重启使用旧令牌的 API/Worker，不要只重启一侧。

### 14.3 验证与故障边界

环境启动后执行真实解析自检（容器内动态生成自制扫描 PDF、PNG/JPEG、双页 TIFF 与简单表格）：

```powershell
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml exec deepdoc /opt/ks-sidecar-venv/bin/python -m deepdoc_sidecar.smoke
```

逐项输出 blocks 数量、正文/表格/页码检查结果，不保存 OCR 正文；任一项失败退出1。
这是最小解析冒烟测试，不替代复杂多栏/跨页表格及完整业务链路验收。

- `backend/scripts/check.py` 包含边车 lint/类型检查、协议单测与真实数据库/本机 HTTP 协议集成测试。
  测试服务明确使用自制响应夹具，只验证集成、警告、引用、授权和删除，不代表识别效果。
- 真实模型验收须在新建的独立知识库中上传自制扫描 PDF、PNG/JPEG、多帧 TIFF、
  多栏 PDF 和跨页表格；人工逐项核对提取文本、页码、所有 positions、表头/行列关系及 warnings。
  再验证旧内容不受失败新版本影响、引用可读、他人 ID 与删除后返回404；不得使用甲方资料替代。
- 不要对旧 build 改库：原 reindex 保留原 parser_profile，不能用来偷换算法。
  升级策略用新库重导入；同版本跨策略迁移待另评审。
- 服务不可达、忙碌、模型缺失或无效输出统一在主任务表现为现有 `PARSE_FAILED`，
  不调用 pypdf 兜底，不变成 `no_evidence`。当前按既有确定性解析失败策略不自动重试；
  修复服务后用现有人工重建恢复。加密文件仍为 `PASSWORD_PROTECTED`。
- 本机 Docker Desktop 4.97.0 的 D 盘程序、独立配置目录及实际 WSL 数据盘已核验，Linux 引擎可用。
  镜像已构建并跑过真实推理（2026-10-27）：自制版面 PNG/JPEG/TIFF/PDF 四格式字符全部识别正确，
  表格与跨帧页码正确，但**词间空格在整行文本中丢失**，导致 `deepdoc_sidecar.smoke` 的正常断言
  4/4 失败；另有适配层 warnings 被全局共享的缺陷。记录与证据见
  `reports/deepdoc-parse-validation-2026-10-27.html`。
  **不能据此宣告 DeepDoc 移植验收完成**：多栏、跨页表格、复杂公式与真实扫描件仍未验证，
  容量与吞吐也没有实测结论。

## 15. 外部数据库（可选）

默认每次运行都创建临时库。要让服务与验证改用已有 PostgreSQL（例如远端服务器），在 `backend/.env`
写一行 `KS_DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:5433/<db>`：
`ask_local.py` 与 `browser_smoke.py` 检测到这一行被显式配置后，就不再创建临时库，改为对该库执行迁移。
该行被注释掉即回到临时库。使用外部库时运行环境自动把租约放宽到 60 秒、心跳 15 秒
（临时库仍是 8/5 秒）：每次心跳都是一个网络往返，秒级租约会在发布前过期并让任务被判取消。

配置 `KS_TUNNEL_SSH=<user>@<host>`（可选 `KS_TUNNEL_KEY`）后，启动器在数据库端口未监听时
自动建立隧道，无需手动执行下面的命令；隧道随启动器结束保留，下次直接复用。

远端库不要直接暴露公网，用隧道把本机端口转到服务器回环地址：

```bash
ssh -N -L 5433:127.0.0.1:5432 root@<server>
```

浏览器验收用**独立库**：它使用桩模型 `hash-1024`，而全库共用一个向量索引，与真实模型的 `bge-m3` 混用会被 `guard_vector_index` 拒绝（任务表现为 `MODEL_UNAVAILABLE`）。
用 `KS_SMOKE_DATABASE_URL` 指定验收库；未配置时退回临时库。两个库不要互换使用。

前提：库内已启用 `vector` 与 `pg_trgm`（扩展需超级用户创建一次）；应用角色对该库有 CREATE 权限。
演示数据会留在该库，重复运行浏览器验收前先重置 `public` schema 再重建扩展，否则脚本内的数量断言
会被历史数据打乱。连接串含口令，只写进被忽略的 `.env`，不提交、不写日志。

2026-10-27 实测：Ubuntu 22.04 + PostgreSQL 16.15 + pgvector 0.8.7，经隧道完成集成用例与浏览器全链路。
