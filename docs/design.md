# 知识检索与智能问答助手：后端设计

_二创项目 · v0.3 · 2026-10-23 · 实现规格_

> 本文定义系统是什么、由哪些部分组成。为什么这样定见 [decisions.md](decisions.md)，怎么跑起来见 [runbook.md](runbook.md)，选型依据见 [background.md](background.md)。文档间有出入以实现本文为准。

---

## 1. 定位与范围

### 1.1 服务定位

知识服务模块：在个人授权范围内提供文档上传、异步解析、内容检索、基于证据的问答与引用、资料更新与删除。以 REST 对外，可被主系统、业务助手或 Agent 宿主调用。

服务不包含 Agent 能力——不编排工作流、不调用外部工具、不维护对话策略。它是被调用方。

### 1.2 提供的能力

| 能力 | 内容 |
| --- | --- |
| 知识库管理 | 个人知识库的创建、列出、删除 |
| 文档导入 | TXT、MD、CSV、文本型 PDF、DOCX、XLSX、PPTX 单文件上传；可选 DeepDoc 扩展见 §2.1；同库同字节去重 |
| 异步处理 | 解析 → 分段 → 向量化 → 原子发布；任务查询；有限重试；崩溃恢复 |
| 内容检索 | 关键词与语义双路召回，RRF 融合；返回来源、定位与实际模式 |
| 智能问答 | 显式知识范围；证据约束生成；结构化引用；无证据显式返回 |
| 来源预览 | 分块预览，反查文档、版本与原文位置；DOCX 另提供段落/表格结构化预览 |
| 生命周期 | 新版本上传、索引重建、删除（逻辑阻断 + 异步清理） |
| 安全 | 令牌鉴权、按所有者的资源隔离、内容级格式校验、日志脱敏 |
| 接入准备 | QwenPaw 的 MCP 适配层与 Skill 包契约 |

### 1.3 范围边界

以下能力不在本版本内，需求出现时按 §10.2 单独评估，不夹带实现：

部门库与跨用户共享 · 网页抓取 · 重排模型与多步检索 · 多轮指代消解与冲突裁定 · 分块手工编辑、QA 对导入、知识图谱 · Office 保真预览 · 账号体系与 SSO · 答案流式输出

2026-10-27 经用户明确评审同意：扫描件 OCR、图片与 PDF 表格识别作为可选 DeepDoc 扩展，
例外范围只在 §2.1 定义。AGENTS 原首期排除条款在此范围内由本规格覆盖，不扩大其他边界。

---

## 2. 技术选型

原则：优先复用成熟组件与标准库，自研只覆盖业务契约本身。

| 层 | 选择 | 依据 |
| --- | --- | --- |
| HTTP | FastAPI + Pydantic | 自带 OpenAPI，REST 与 MCP 适配层共用模型定义 |
| 存储 | PostgreSQL 单实例 | 业务表、任务表、向量、关键词共库，发布与清理天然同事务 |
| 向量 | pgvector，HNSW，余弦距离 | 与元数据联合过滤，无需独立中间件 |
| 关键词 | pg_trgm，`gin_trgm_ops` | 中文无需分词器；对文号、专名、精确短语有效 |
| 任务 | 任务表 + `FOR UPDATE SKIP LOCKED` | 任务与业务同事务提交，无消息双写 |
| 解析 | TXT/MD/CSV 自读；PDF 用 pypdf；DOCX 用 python-docx；XLSX 用 openpyxl；PPTX 用标准库读 OOXML | 纯 Python，无外部二进制；PPTX 只取文本，不还原版式 |
| 向量化 | BGE-M3，1024 维 | 中文稳定，维度固定便于约束 |
| 生成 | 经 GeneratorAdapter 接入既有推理服务 | 不绑定具体模型 |

同一时间只启用一个向量配置，维度写入迁移。更换模型即新建索引并重建，新旧向量不混检。

模型适配器复用 Pydantic 校验响应结构。上游响应封装无效归为 MODEL_UNAVAILABLE，
生成内容不符合 sections/evidence_keys 约定归为 CITATION_VALIDATION_FAILED，不伪装成 no_evidence。
向量响应须数量、维度和有限数值有效；提供 index 时须完整、唯一且连续，并按 index 恢复输入顺序。

### 2.1 可选 DeepDoc 解析扩展

- 复用 RAGFlow **v0.22.0** 的 `RAGFlowPdfParser.parse_into_bboxes`，不复制整个平台业务，
  不接入它的账号、任务、向量库或生成链。上游源码、依赖与预置模型复用独立镜像，
  主后端不安装 OCR/ONNX 等重依赖；边车部署源码位于 `backend/deepdoc_sidecar/`。
- 默认 `legacy-v1` 保持原处理行为；显式启用 `deepdoc-v0.22.0-v1` 后，PDF（含扫描件）和
  PNG/JPEG/TIFF 送往**同机**边车。图片先本地转为 PDF，再进入同一 OCR/版面/表格链；
  多帧 TIFF 保留帧顺序。TXT/MD/DOCX 仍用现有解析器，DOCX 复杂结构不在本次范围。
- 引用保留上游所有页位置 `positions = [[page, left, right, top, bottom], ...]`，页码从 1 开始，
  坐标为上游 PDF 页面坐标。`page` 是首个位置页，`precision=block`；字符区间是**提取块文本**
  的码点偏移，不冒充原文件字节偏移或精确字框。子片段保留原块位置，跨页块不裁成假的单页。
- 表格结构先由 DeepDoc 识别，再将 HTML 按行、制表符列分隔转换为证据文本；不返回可执行 HTML，
  不宣称完整恢复合并单元格语义。不引入视觉生成模型；图形内容不作自动描述。
- 每个新 build 的 `chunk_config.parser_profile` 记录处理策略；无该键的历史构建按 `legacy-v1`。
  重试与现有 reindex 复制原配置，不受运行期默认值改变影响；已发布块不改动。
  该字段不改变字符切分策略，不能把 `max_chars` 宣称为 token 上限。无需 DDL 迁移。
- 原版本不能借现有 reindex 偷换解析策略；体验新解析请在新建知识库中重新导入。
  后续若需同版本跨策略迁移，必须另评审契约，不修改原重建语义。
- `ParsedBlock.warnings` 随发布写入 build，检索、问答和分块读取返回相关警告；
  检索 `degraded` 在命中带警告时为 true。新增响应字段仅为可选 `warnings`，既有错误码不变。
  固定警告码：`deepdoc_ocr_requires_review`（OCR 需核对）、`deepdoc_table_linearized`（表格线性化）、
  `deepdoc_figures_not_described`（图形未描述）、`deepdoc_empty_page`（有页面未提取可用正文）。
- 边车只接受受信任 Worker 经令牌提交的原始文件，不收路径、URL、原文件名或自述身份。
  本机回环端口发布、容器内部网络禁外连、不挂载业务库与对象存储、不启动上游平台服务。
  部署入口复用同镜像 Nginx 固定反向代理：网关连接入口和内部网络，仅转发 health/parse；
  解析容器仅连接内部网络，无外网默认路由。网关不是可传入任意目标的 HTTP 代理。
  文件内容只在内存处理；子进程可终止，输入/输出/页数/像素/CPU/RSS/耗时均有限制。
  配置上限是保护阈值，不是性能或容量承诺；错误不包含第三方输出，禁止静默回退。
- 固定上游 release 不等于模型权重已做独立指纹验收；部署应进一步固定镜像 digest，
  模型变更须新建处理策略版本。健康接口仅表示 HTTP 存活，实际推理验收见 runbook §14。

上游依据：[PDF 解析器](https://github.com/infiniflow/ragflow/blob/v0.22.0/deepdoc/parser/pdf_parser.py)、
[上游镜像构建](https://github.com/infiniflow/ragflow/blob/v0.22.0/Dockerfile)。

---

## 3. 架构

开发工作台：按用户明确需求新增根目录 `frontend/`，参考 RAGFlow 的知识库、检索测试和问答分区，
通过既有 REST 接口工作；由 API 同源静态托管到 `/workbench/`，不增加业务接口或身份语义。
首版采用浏览器原生模块、Fetch、dialog 与 CSS，不增加前端构建服务或生产依赖。
该工作台不是前端富预览、账号平台或 QwenPaw 接入；引用仅展示授权后的原文分块。

```
调用方：主系统 / 业务助手 / QwenPaw Agent
        │ REST（OpenAPI）          ┌─ MCP 适配层（§9）
        ▼                          ▼
   api/v1  ───────────────────►  services  ────►  adapters
                                  （解析 / 向量 / 生成 / 存储）
                                     │
                   ┌─────────────────┴─────────────────┐
                   ▼                                   ▼
             PostgreSQL                          文件卷（原件）
     业务表 + 任务表 + pgvector + trgm         object_key 由服务端生成
                   ▲
                   │ 读取同一张任务表
             worker（单进程，并发 1）
```

运行单元：`api`、`worker`、`postgres`，加一个文件目录。

`services` 层独立于传输方式：不 import FastAPI，不接收 `Request` 对象；REST 与 MCP 两个入口共用同一套业务方法。

### 3.1 代码分层

```
backend/
  knowledge/                      可安装包
    entrypoints/                  进程入口，每个只有一个装配函数
      api.py                      create_app()
      worker.py                   main()
      mcp.py                      main()
    api/
      deps.py                     Actor 与 DB session 注入
      errors.py                   领域异常 → HTTP 错误映射
      v1/
        routers/                  按资源拆分
        schemas/                  请求与响应模型
    mcp/                          协议适配，与 api/ 平级
      tools.py                    6 个工具的参数与错误映射
    services/                     业务编排、事务边界
    worker/                       任务领取循环与租约管理
      loop.py                     claims → heartbeat → 异常转重试
    domain/                       内层，不依赖框架
      entities.py  ports.py  errors.py  chunking.py
    repositories/                 PostgreSQL 访问
      tables.py  session.py       表定义与会话
    adapters/                     外部系统
      parser.py  embedding.py  generator.py  storage.py
    config.py  security.py  logging.py
  migrations/
  tests/
    unit/  integration/  e2e/
    conftest.py
  testdata/                       自制测试资料与标注
  integrations/
    qwenpaw/                      Skill 包与工具契约测试
```

依赖方向：`entrypoints → {api, mcp, worker} → services → domain ← repositories / adapters`。`domain` 不 import 框架或第三方 SDK；`services` 单向依赖（`qa → retrieval → repositories`），同层不得互调成环。

`api`、`mcp`、`worker` 是三个并列的协议/执行适配层，彼此不互相 import，只共享 `services`。

### 3.2 放置规则

| 位置 | 只放 | 不得放 |
| --- | --- | --- |
| `entrypoints/` | 启动装配、依赖注入 | 业务逻辑 |
| `api/` | 参数校验、鉴权入口、响应组装、异常映射 | 业务判断、SQL |
| `api/v1/schemas/` | 请求与响应模型 | 领域实体、数据库字段 |
| `mcp/` | MCP 工具的参数、返回与错误映射 | 业务逻辑、直接写 SQL |
| `services/` | 用例编排、事务边界 | SQL、SDK 调用、`Request` 对象 |
| `domain/` | 实体、值对象、枚举、不变量、端口 Protocol | 框架类型、SQLAlchemy、Pydantic |
| `repositories/` | SQL、表定义、事务 | 外部 HTTP、文件存储 |
| `adapters/` | 外部系统的协议实现 | 业务判断 |
| `worker/` | 领取循环、租约与心跳、异常转重试 | 业务规则本身（放 `services/`） |

`repositories` 与 `adapters` 都做持久化，分工是：前者只碰 PostgreSQL，后者只碰外部系统（文件、模型、解析器）。文件读写出现在 `repositories` 里即为错位。

### 3.3 命名规则

1. 目录用复数表示「多个同类实现」（`adapters/`、`routers/`、`schemas/`）；表示单一职责模块的目录用单数（`worker/`、`mcp/`）。模块本身一律单数（`parser.py`）。
2. 一个模块只放一类对象：实体、表定义、端口、用例各自独立，不混装。
3. 领域实体叫 `entities.py`，数据库表定义叫 `repositories/tables.py`——不出现两个 `models.py`。
4. 不设 `utils/`、`common/`、`helpers/`。需要共用代码时，按归属层放回对应目录。

---

## 4. 数据模型

八张表。均含 `created_at`（UTC）与 UUID 主键；JSON 字段为 `jsonb`。

### 4.1 `knowledge_bases`

`id` · `owner_id`（来自令牌，不接受请求体传入）· `name` · `status` enum(active, deleting, deleted)
索引：`(owner_id, status)`

### 4.2 `documents`

`id` · `kb_id` · `title` · `status` enum(active, deleting, deleted)
`current_version_id` · `active_build_id` · `pending_build_id` · `generation` int

文档是用户认知中的「这份资料」，标题变化后身份不变；`generation` 每次更新或删除递增，用于发布栅栏。
索引：`(kb_id, status)`

### 4.3 `document_versions`

`id` · `kb_id`（冗余，仅用于去重索引）· `document_id` · `version_no` · `object_key` · `sha256` · `media_type` · `byte_size`

唯一：`(document_id, version_no)`、`(kb_id, sha256)`。后者实现同库同字节不重复建版本。

### 4.4 `index_builds`

`id` · `version_id` · `state` enum(queued, running, ready, failed, cancelled, retired)
`chunk_config` jsonb · `embedding_model` · `embedding_dim` · `warnings` jsonb

一次索引构建；重建索引不产生新版本。`state = ready` 后不可修改。`warnings` 记录解析降级（如「未提取到页码」）。

### 4.5 `chunks`

`id` · `build_id` · `ordinal` · `text` · `locator` jsonb · `warnings` jsonb · `embedding` vector(1024) · `tsv` text

索引：唯一 `(build_id, ordinal)`；`GIN (tsv gin_trgm_ops)`；`HNSW (embedding vector_cosine_ops)`

### 4.6 `locator` 结构

```json
{ "type": "pdf", "page": 2, "block": 4,
  "char_start": 0, "char_end": 11, "precision": "block" }
```

页码从 1 开始；字符偏移按 Unicode 码点计数、左闭右开。DOCX 用 `{"type":"docx","paragraph":37}`，`page` 为 `null`。
XLSX 用 `{"type":"xlsx","sheet":"资产清单","row_start":1,"row_end":9,"precision":"sheet"}`：
行号从 1 开始、含表头，跳过空行但仍按真实 Excel 行号给出区间；块内字符偏移不参与阅读定位。
无法定位到字符时降级为 `block` 或 `page`。`chunks.warnings` 按块保存解析降级信号（OCR 需复核、表格已线性化、
图片未描述、存在空白页），build 级 `warnings` 是全部块的并集；只看并集无法判断哪一块受影响，前端与检索
据此只提示受影响的片段。

### 4.7 `jobs`

`id` · `kind` enum(ingest, reindex, cleanup) · `resource_id` · `state` enum(queued, running, succeeded, failed, cancelled)
`stage` · `attempt` / `max_attempts`（默认 0/3）· `available_at`
`lease_token` · `lease_expires_at`（领取后 120 秒）· `heartbeat_at`（30 秒一次）· `last_error` jsonb

`build_id` · `generation`：ingest/reindex 任务创建时固定目标构建与文档代次；cleanup 不填。
每次尝试期间目标不可改变。重试保留 job ID 与累计 attempt，但新建同版本 build，
在同一事务切换 job.build_id 和 document.pending_build_id；generation 不变。
旧构建置 failed，ready 构建绝不重写。旧代次任务取消，不接管新上传的构建。

`last_error` 存 `{code, message, retryable}`，不含堆栈。
索引：`(state, available_at)`、`(lease_expires_at)`

### 4.8 `conversations` / `messages`

`conversations`：`id` · `owner_id` · `title`
`messages`：`id` · `conversation_id` · `role` · `body` · `answer_status` · `citations` jsonb · `model_revision`

`citations` 每项：`{index, document_id, version_id, build_id, chunk_id, title, excerpt, locator}`。`excerpt` 由服务端从分块原文截取。
`/qa` 带 `conversation_id` 时把本轮问题与答案写入消息；`GET /conversations` 与
`GET /conversations/{id}` 按 owner 读取历史，供页面重连后恢复。历史只用于展示，
不参与检索或生成上下文。

### 4.9 不变量

六条硬约束，每条都有对应测试：

1. 所有查询强制带 `knowledge_bases.owner_id = 当前 subject`；无权与不存在统一返回 404。
2. 内容可检索当且仅当：库 active ∧ 文档 active ∧ `chunk.build_id = documents.active_build_id` ∧ `build.state = ready`。
3. 发布只能由「持有有效 `lease_token` ∧ `generation` 匹配 ∧ `pending_build_id` 未变」的任务完成。
4. `state = ready` 的 build 及其 chunks 只读；重试与重建一律新建 build。
5. 引用只能指向本轮实际送入模型的 chunk。
6. 删除事务提交后，新的检索、问答与预览请求一律不可见；清理失败不影响阻断。

---

## 5. 处理流程

### 5.1 上传

1. 校验令牌、库归属、格式白名单、大小上限（50 MiB）。
2. 流式写临时文件并算 sha256，按内容签名确认类型，不依赖扩展名。
3. `(kb_id, sha256)` 已存在时返回已有 `document_id` 与 200。
4. 写对象存储（object_key 服务端生成），单事务创建 `document / version / build / job`，`generation` 递增，`pending_build_id` 指向新 build。
5. 返回 202，含 `document_id, version_id, build_id, job_id`，不等待解析。

对象写入成功而事务失败时，由启动扫描回收创建超过 1 小时且无引用的对象。

### 5.2 构建状态机

```
queued → running(parsing → chunking → embedding → publishing) → ready → retired
   │         │
   ├─────────┴→ failed ──(attempt < 3 且错误可重试)──→ queued
   └─────────┴→ cancelled（文档已删除，或已被更新的 generation 取代）
```

图中重试箭头表示 job 的下一次尝试创建一个新的 queued build，不是原 build 回到 queued。

发布在单事务内完成：校验租约、`generation` 与文档状态 → `build.state = ready` → `current_version_id` 与 `active_build_id` 同时切换 → 原 build 置 `retired` → 任务置 `succeeded`。

首次上传发布前 `active_build_id` 为空，此时检索该文档返回 0 命中并提示「处理中」。更新期间原 build 继续服务。

### 5.3 Worker

```sql
UPDATE jobs SET state = 'running', attempt = attempt + 1,
       lease_token = gen_random_uuid(),
       lease_expires_at = now() + interval '120 s'
WHERE id = (
  SELECT id FROM jobs
  WHERE state = 'queued' AND available_at <= now()
  ORDER BY available_at LIMIT 1
  FOR UPDATE SKIP LOCKED)
RETURNING *;
```

- 领取在短事务内完成，提交后才执行解析与模型调用。
- 心跳由独立协程每 30 秒续租，不受解析或模型调用阻塞。
- 每次写 chunks 前校验 `lease_token` 仍匹配。
- 启动及每轮领取前回收过期租约；attempt 不减，到达上限置 failed，否则按新 build 重试。
- 写入使用 document → job → build 的锁顺序；校验 token、running 与未过期租约，并在同一短事务内写入及发布。心跳不得复活过期租约。
- 格式不支持、密码保护、空正文属确定性错误，直接置 `failed`。

### 5.4 删除

| 阶段 | 动作 | 对外表现 |
| --- | --- | --- |
| 逻辑删除（同步事务） | 置 `deleting`、`generation` 递增、清空发布指针、取消未完成任务、建 cleanup job | 立即不可检索、不可预览、引用不可读 |
| 物理清理（异步 job） | 删除原件、chunks、builds、versions，置 `deleted` | 在线数据清理完成 |

删除知识库时先阻断整库，再逐文档建 cleanup job。

---

## 6. 检索与问答

### 6.1 检索

参数：`query`（≤2000 码点）、`knowledge_base_ids`（必填，1–20）、`top_k`（默认 5，≤20）、`mode`（hybrid / keyword / semantic）。

两路召回都在各自子查询内部带上授权条件，不做「先取 Top K 再过滤」。

- 关键词路：按 `tsv` 与查询的 trigram 相似度排序，同分按 `ordinal`，低于相似度下限的丢弃。
- 语义路：按 `embedding <=> 查询向量` 的余弦距离排序，超过距离上限的丢弃。
- 融合：`score = Σ 1/(60 + rank_i)`，取 Top K；两路都未命中的分块不进入结果。

「未命中即无答案」依赖这两个阈值，而不是 RRF 分数本身——RRF 分数只表示名次，恒大于零，不能用来判断相关性。距离上限默认值按本地哈希向量标定，换真实向量模型后必须重新标定。

命中返回 `document_id / version_id / build_id / chunk_id / title / excerpt / locator / score / score_type`。`score` 只在同一模式下可比，响应必须带 `score_type`。

semantic/hybrid 调用模型前检查授权范围内已发布构建的模型标识与维度，召回 SQL 也限定相同配置。
不兼容返回既有 `MODEL_UNAVAILABLE`（503），不冒充 no_evidence，不自动降级；显式 keyword 仍可使用。
写入前通过事务级配置锁检查现有 chunks 所属构建，禁止不同模型/维度共用当前物理向量索引，
包括 retained 的旧构建；更换模型须离线迁移索引，不支持原索引内渐进混写。

### 6.2 问答

```
校验身份与范围 → 检索 → 证据门槛 → 生成 → 引用校验 → 授权复核 → 返回
```

1. **证据门槛**：命中为 0 或最高融合分低于阈值（可配，按测试集校准）时返回 `no_evidence`，不调用生成模型。
2. **生成**：传入 `E1…En` 证据键，要求输出 `{"sections":[{"text":"…","evidence_keys":["E1"]}]}`。
3. **引用校验**：键必须属于本轮输入，未知键丢弃；全部段落无有效键则返回 `CITATION_VALIDATION_FAILED`（502），不返回正文；部分段落无键则保留答案并在 `warnings` 说明；`excerpt` 由服务端截取。
4. **授权复核**：重查引用文档是否仍 active、`active_build_id` 是否变化；变化时返回 `SOURCE_CHANGED`（409）。
5. **应答状态**：`answered` / `no_evidence` / `conflicting_evidence`。模型超时或不可用按错误返回，不归入 `no_evidence`。

`conversation_id` 用于存档；生成只使用当前问题与本轮证据，不从历史答复取证据。

---

## 7. API 契约

前缀 `/api/v1`；`Authorization: Bearer <token>`；JSON 用 `snake_case`；时间为带时区 ISO 8601。

| # | 方法与路径 | 说明 | 成功码 |
| --- | --- | --- | --- |
| 1 | POST `/knowledge-bases` | 建库，owner 取自令牌 | 201 |
| 2 | GET `/knowledge-bases` | 列出本人知识库 | 200 |
| 3 | DELETE `/knowledge-bases/{kb_id}` | 阻断并异步清理 | 202 |
| 4 | POST `/knowledge-bases/{kb_id}/documents` | multipart 上传 | 202；去重命中 200 |
| 5 | GET `/knowledge-bases/{kb_id}/documents` | 列表，含处理状态 | 200 |
| 6 | GET `/documents/{document_id}` | 当前版本、pending build、最新任务 | 200 |
| 7 | POST `/documents/{document_id}/versions` | 上传新版本，冲突 409 | 202 |
| 8 | DELETE `/documents/{document_id}` | 逻辑删除并建 cleanup job | 202 |
| 9 | GET `/jobs/{job_id}` | state / stage / attempt / progress / error | 200 |
| 10 | POST `/search` | 独立检索，不调用生成模型 | 200 |
| 11 | POST `/qa` | 问答 | 200 |
| 12 | GET `/chunks/{chunk_id}` | 分块预览，带授权校验 | 200 |
| 13 | POST `/conversations` | 创建会话，用于问答存档 | 201 |
| 14 | POST `/documents/{document_id}/reindex` | 最新上传版本的同模型重建/失败恢复，见 §7.1 | 202 |
| 15 | GET `/documents/{document_id}/chunks` | 分块结果视图：当前已发布 build 的片段、定位与总数，`limit` 默认 200 | 200 |
| 16 | GET `/documents/{document_id}/file` | 原始文件字节：当前版本原件，PDF/图片 `inline`，其余 `attachment`；带 `nosniff` 与 `private, no-store`，他人与已删除 404 | 200 |
| 17 | GET `/documents/{document_id}/file?as=png` | 位图转码预览：TIFF 等浏览器不支持的格式转 PNG 后 `inline` 返回（多页只取首页、最长边 2400）；原件不变，非图片 415 | 200 |
| 18 | GET `/documents/{document_id}/sheets` | 表格只读预览：按工作表返回前 200 行 / 60 列与截断标记，供前端画表格；支持 XLSX 与 CSV，其他 415 | 200 |
| 19 | GET `/conversations` | 列出本人会话（按创建时间倒序） | 200 |
| 20 | GET `/conversations/{conversation_id}` | 会话与消息历史；他人与会话不存在统一 404 | 200 |
| 21 | GET `/documents/{document_id}/docx` | DOCX 只读结构化预览：段落与表格按正文顺序返回，不保留字体/分页/图片；非 DOCX 415 | 200 |

`POST /search` 的每条 `hits` 带 `kb_id` 与 `kb_name`，用于在结果中标注所属知识库。

**成功**：`{"data": {…}, "meta": {"request_id": "…"}}`
**错误**：`{"error": {"code": "…", "message": "…", "retryable": bool}, "meta": {"request_id": "…"}}`

| HTTP | code |
| --- | --- |
| 401 | `UNAUTHENTICATED` |
| 404 | `RESOURCE_NOT_FOUND`（无权与不存在不区分） |
| 409 | `VERSION_CONFLICT` / `SOURCE_CHANGED` / `STALE_JOB` |
| 413 / 415 | `FILE_TOO_LARGE` / `UNSUPPORTED_FORMAT` |
| 422 | `VALIDATION_ERROR` |
| 502 | `CITATION_VALIDATION_FAILED` |
| 503 / 504 | `MODEL_UNAVAILABLE` / `MODEL_TIMEOUT` |

`/qa` 请求与响应（示例内容为测试资料）：

```json
{ "question": "会议室使用后需要完成哪些事项？",
  "knowledge_base_ids": ["…"], "top_k": 5, "conversation_id": null }
```

```json
{ "data": {
    "answer_status": "answered",
    "answer": "使用后应清理现场并关闭设备。[1]",
    "citations": [{ "index": 1, "title": "会议室管理办法（测试资料）",
                    "document_id": "…", "version_id": "…", "build_id": "…",
                    "chunk_id": "…", "excerpt": "使用后应清理现场，并关闭设备。",
                    "locator": { "type": "pdf", "page": 2, "precision": "block" } }],
    "retrieval": { "mode": "hybrid", "hits": 5, "degraded": false },
    "warnings": [] },
  "meta": { "request_id": "req_demo" } }
```

---

### 7.1 同模型重建与人工恢复

请求 JSON：`{"version_id": "UUID", "expected_generation": 1}`，两项必填，代次为非负整数。
成功 data：`document_id / version_id / build_id / job_id / generation`，返回 202。
禁止额外字段，不接收 owner、tenant、模型配置或文件路径。

1. 校验文档及知识库均 active 且归属调用者；无权、不存在、已删除或删除中均为 404。
2. 锁定文档并复核 generation；目标必须是该文档最新上传版本（包括处理失败者），不提供历史回滚。
3. 存在 queued/running 处理任务、代次过期、目标非最新、向量配置不一致，返回 409 VERSION_CONFLICT。
   不取消原任务，不创建额外构建；不属于该文档的 version_id 统一为 404。
4. 新建 build 与 reindex job，generation 递增，job 固定新代次与 build，attempt 从 0 开始。
   原件和 version 不变，沿用原构建的分段配置与向量模型/维度；不支持在线更换向量配置。
5. 写入同一事务提交；重建期间旧 active build 继续提供检索/引用，失败不撤销旧发布内容。
   成功复用 §5.3 原子发布规则；旧 build 仅按该规则 retired，不修改其片段。
6. 重复旧请求仍返回 409，不另建幂等记录；调用方刷新文档详情、查询已有 job，不能盲目重发。

本节中的 generation 递增属于文档更新；附录 A 与 §4.2 的“更新”包括显式重建。
最初上传失败时无 current_version_id，调用方使用上传回执的 version_id；
新版本失败时也使用该次上传回执，而非旧的 current_version_id。重建不替代不可恢复资料的重新上传。

## 8. 安全

| 项 | 实现方式 |
| --- | --- |
| 身份 | HMAC 签名令牌，载荷 `{subject_id, tenant_id, exp}`；密钥由环境注入，不入库 |
| 授权 | repository 方法显式接收 `Actor`，SQL 内强制 `owner_id`；不存在绕过授权的公共查询入口 |
| 文件 | 格式白名单 + 内容签名校验；object_key 服务端生成；文件名不参与路径拼接 |
| 解析 | 受 CPU 与时间上限约束；DOCX 限制解压总量与文件数；不解析外链 |
| 模型 | 端点走服务端白名单；文档内容按不可信输入处理，模型无工具权限 |
| 日志 | 记录 `request_id / job_id / build_id`；不记录问题正文、片段、答案、令牌 |
| 密钥 | 仅经环境变量注入，不入库、不入日志、不入测试资料 |

接入真实资料前补齐审计表、保留期策略与配额限流，见 §10.2。

任务错误使用固定错误码/说明白名单，历史 last_error 在输出时再次脱敏；日志不序列化异常正文或堆栈参数。
解析在可终止子进程执行，使用 psutil 采样 CPU/RSS 并超限终止，同时限制运行时间、返回大小及 DOCX 条目数/解压量。
采样式限制允许采样间隔内短暂超额，不等同于操作系统硬内存隔离；部署硬配额仍由容器或宿主负责。

---

## 9. QwenPaw 接入准备

> QwenPaw（AgentScope 团队，Apache-2.0，`Python >=3.11,<3.14`，当前 v2.2.x）的扩展方式为 Skills（目录 + `SKILL.md`）、Plugins、MCP、REST API 与 ACP。Skill 通过 `metadata.requires.mcp` 声明依赖的 MCP 服务，配置以 `QWENPAW_SKILL_CONFIG_<NAME>` 环境变量注入。

接入方式为 **MCP 服务 + Skill 包**：本服务对 QwenPaw 呈现为一组 MCP 工具，通过 `SKILL.md` 挂载，QwenPaw 源码不改动。接入形态的取舍见 [decisions.md](decisions.md) 0008。

### 9.1 接口约束

1. `services` 层不依赖 HTTP，MCP 适配层只做参数转换与错误映射。
2. 工具面固定 6 个，与 §7 对应：

   | 工具 | 接口 |
   | --- | --- |
   | `kb_list` | GET `/knowledge-bases` |
   | `doc_ingest` | POST `/knowledge-bases/{kb_id}/documents` |
   | `job_status` | GET `/jobs/{job_id}` |
   | `search` | POST `/search` |
   | `ask` | POST `/qa` |
   | `chunk_get` | GET `/chunks/{chunk_id}` |

3. 每次调用返回终态或明确的任务 ID，不要求调用方保存游标。
4. 返回结构化数据，不返回 HTML、内部 URL 或绝对路径。

### 9.2 身份映射

- `subject_id` 由集成侧写入 Skill 配置（`QWENPAW_SKILL_CONFIG_KB_SERVICE` 的 `subject_id` 与 `token`），服务从令牌解析。
- 不从对话内容、工具参数或用户自述推断身份；工具签名不接收 `owner_id`。
- 一个 QwenPaw 实例映射一个 `subject_id`；多用户按实例隔离。

### 9.3 与 ReMe 记忆的边界

ReMe 保存对话记忆（可读写的 Markdown），本服务保存权威文档语料：

- 引用只指向 `document → version → build → chunk`，记忆内容不作为证据。
- 不读写 QwenPaw 工作区文件，文档入库一律经 `doc_ingest`。

### 9.4 落点

```
integrations/qwenpaw/
  skill/SKILL.md          # name / description / metadata.requires.mcp
  skill/README.md         # 安装与配置说明
  mcp_server.py           # services → MCP 工具的薄适配
  contract_test/          # 对固定 QwenPaw 版本的工具契约测试
  PINNED_VERSION          # 适配的 QwenPaw 版本范围
```

升级 QwenPaw 时运行 `contract_test/`，校验工具名、参数与错误码兼容性。

`SKILL.md` 模板：

```markdown
---
name: knowledge-service
description: 在个人知识库中检索资料并给出带引用的答案。当用户询问"某份文件里怎么规定的""查一下制度"时使用。
metadata:
  version: "0.1"
  requires:
    env: [KB_SERVICE_URL, KB_SERVICE_TOKEN]
    mcp: [knowledge-service]
---
# 何时使用
- 需要依据已上传文件作答时使用；闲聊与需要联网实时信息的问题不使用。

# 使用约束
- 必须传入 knowledge_base_ids，检索范围不超出调用方给定的知识库。
- 引用由服务返回；不自行编写出处，不将记忆内容作为引文。
- 返回 no_evidence 时告知"资料中未找到依据"，不补充推测内容。
```

---

## 10. 验收与扩展点

### 10.1 验收用例

| ID | 断言 |
| --- | --- |
| M01 | 建库 → 上传 TXT → 轮询 job 至 succeeded → 检索命中预期原文 |
| M02 | 问答引用可经 `/chunks/{id}` 定位到对应版本与页码 |
| M03 | 无答案问题返回 `no_evidence`，日志中无生成模型调用记录 |
| M04 | 用户 B 使用 A 的 kb / document / job / chunk / conversation ID 访问，均 404 |
| M05 | v2 处理中检索返回 v1 内容；发布后仅返回 v2 |
| M06 | 解析中终止 Worker，重启后任务恢复，chunk 不重复 |
| M07 | 处理中删除文档，重试不使其复活；cleanup 完成后原件不可读 |
| M08 | 同库重复上传同字节不产生新版本；同名不同字节产生新版本 |

### 10.2 扩展点

| 触发信号 | 扩展动作 |
| --- | --- |
| 关键词路 Recall@K 显著低于语义路 | pg_trgm 换为分词器 + tsvector |
| 加重排后 NDCG 提升超过 10% 且延迟可接受 | 引入 bge-reranker-v2-m3 |
| 需要按文档反查引用 | `citations` 由 JSONB 拆为独立表 |
| PDF 解析失败率超过 20% | 换用 Docling，PDF 走独立重解析队列 |
| 单 Worker 排队中位数超过 5 分钟 | 任务表升级为消息队列，Worker 横向扩展 |
| 需要多轮指代与追问 | 引入会话上下文与检索改写 |
| 需要部门库或共享 | 扩展授权模型与 `AuthorizationAdapter` |
| 真实资料接入 | 补审计表、保留期策略、配额限流 |

---

## 11. 待确认

| 优先级 | 事项 | 当前处理 |
| --- | --- | --- |
| P0 | 主系统身份协议；QwenPaw 侧 `subject_id` 写入责任方 | 本地开发用签名令牌，接入真实身份时替换验证器 |
| P0 | 真实资料外发边界与模型服务范围 | 仅使用测试资料 |
| P0 | 审计与保留期要求 | 接入真实资料前补齐 |
| P1 | 是否允许自建本地模型 | 按外部推理服务设计 |

---

## 附录 A 术语

术语只在此定义，代码与文档沿用。

| 术语 | 含义 |
| --- | --- |
| `document` | 用户认知中的这份资料，标题与版本变化后身份不变 |
| `version` | 一次不可变的上传；正文变化才产生新版本 |
| `build` | 对某版本按一组配置生成的一次索引；重建索引只产生新 build |
| `chunk` | 检索与引用的最小证据单位，属于某个 build |
| `parser_profile` | build 固定的解析策略标记；具体契约见 §2.1，不代表模型权重指纹 |
| `locator` | 分块在原文中的位置；页码从 1 开始，字符偏移按码点、左闭右开 |
| `generation` | 文档级版本号，每次更新或删除递增，用于判定任务是否过期 |
| `lease_token` | 每次领取任务重新生成的凭证，写入前必须校验 |
| `evidence_key` | 本轮证据的键（`E1…En`），由服务端分配，模型只能引用不能编造 |
| `citation` | 结构化引用，绑定 document / version / build / chunk |
| `excerpt` | 引用摘录，由服务端从原文截取，不采用模型改写 |
| `answer_status` | `answered` / `no_evidence` / `conflicting_evidence` |
| `RRF` | 按名次融合两路结果：`Σ 1/(60 + rank)`，不相加原始分数 |

**常见误用**

| 说法 | 正确含义 | 误用后果 |
| --- | --- | --- |
| 「最新版本」 | `current_version_id`，已发布版本 | 把未发布内容当生效内容 |
| 「文档已发布」 | active **且** `active_build_id` 指向 ready 构建 | 检索到空内容 |
| 「重建索引」 | 新建 build，版本不变 | 污染版本历史 |
| 「已删除」 | 逻辑删除提交即不可见 | 把清理延迟当成可访问 |
| 「无答案」 | 证据不足，属成功结果 | 掩盖模型故障 |
| 「引用有效」 | 键属于本轮输入且定位可解析 | 误认为证据支持结论 |
