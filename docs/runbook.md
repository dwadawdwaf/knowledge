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
