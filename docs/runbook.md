# 运行手册

根目录命令均在项目根目录执行；接口语义见[设计](design.md)，未完成项见[issues](../issues.md)。

### 原生 RAGFlow / OpenSearch 搜索

业务数据库仍为 PostgreSQL。准备 Windows OpenSearch 3.9.x 后运行 `./start-opensearch.ps1 -OpenSearchHome <安装目录>`；本机已有安装默认位于 `backend/.data/windows-retrieval-v1/opensearch-3.9.0`。脚本只监听127.0.0.1:19276，不自动下载、不覆盖已运行的服务，使用持久的搜索数据目录。默认开发配置关闭搜索集群认证，仅用于本机。

先在 `backend/.env` 设置 `KS_OPENSEARCH_URL=http://127.0.0.1:19276`、`KS_OPENSEARCH_INDEX=ks-ragflow-native-v2`，再用项目虚拟环境执行：

```powershell
.\backend\.venv\Scripts\python.exe backend/scripts/sync_native_index.py --owner demo-owner
```

`--owner` 必须是目标用户的真实subject；开发工作台默认是 `demo-owner`。工具按该用户当前可访问的发布构建回填本机派生索引，直接复用原分块和向量，不调用模型，重复运行跳过已有完整标记的构建。大库第一次回填可能较慢，查询不负责临时建库。

回填完成后设置 `KS_SEARCH_ENGINE=opensearch` 并重启后端，资料检索和问答共用原生搜索路径。`start.ps1` 在该配置下尝试启动本机搜索服务；搜索服务故障或缺索引不阻断文件管理，检索明确返回 `KEYWORD_INDEX_UNAVAILABLE`，不自动切换策略。健康提示仍按当前用户统计。新上传在最终发布前同步索引，失败保留原发布版本。

映射版本不兼容时换一个新的索引名重新回填，禁止原地覆盖旧索引。回退时将 `KS_SEARCH_ENGINE` 改为 `postgresql` 并重启，保留原生索引方便复查；不会删除业务资料或重算向量。PostgreSQL回退路径仍要求其派生词项已完成回填。原生路径缺索引应运行本节工具，`backfill_ragflow_terms.py` 只修复PostgreSQL词项。

## 1. 安装与配置

解释器版本以.python-version为准，依赖用backend/requirements-dev.lock，不只装pyproject的浮动范围。
首次安装需要允许下载，已有环境不盲目删除或重装：

```powershell
py -3.12 -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.lock
```

py -3.12不保证补丁版本。脚本从源码运行，不要求editable安装。
配置放backend/.env或环境变量，不提交密钥：

| 配置 | 用途 |
| --- | --- |
| KS_EMBEDDING_BASE_URL / KS_EMBEDDING_MODEL / KS_EMBEDDING_API_KEY / KS_EMBEDDING_DIM | 向量服务与1024维配置 |
| KS_GENERATOR_BASE_URL / KS_GENERATOR_MODEL / KS_GENERATOR_API_KEY | OpenAI兼容聊天服务 |
| KS_DATABASE_URL | 显式数据库；根启动器未设置时自动用本机内置库 |
| KS_WORKBENCH_OBJECTS | 根启动器持久原件目录，默认backend/.data/objects |
| KS_STORAGE_ROOT | 手动运行API/Worker的原件目录，不等同根启动器的卷配置 |
| KS_DOCUMENT_PARSER_PROFILE | 非PDF的默认解析策略，只影响新上传/新版本 |
| KS_PDF_PARSER_PROFILE | PDF默认mineru-api4.1-v1，显式选择优先；重建保留原策略 |
| KS_TOKEN_SECRET | 本地开发身份桩签名密钥，正式接入替换验证器 |

模型配置可能向外部服务发送内容，真实资料须另授权。换向量模型必须离线迁移索引、重建和重标阈值，不在现有索引混写。

## 2. 启动与数据保留

```powershell
.\start.cmd
.\start.cmd -Mode Demo
.\start.cmd -CheckOnly
.\start.cmd -NoBrowser -Port 8011
```

| 模式 | 行为 |
| --- | --- |
| 默认Real | 真实模型、持久库及原件卷，退出后资料保留 |
| Demo | 临时隔离库及原件目录、确定性模型桩，退出清理，不代表模型效果 |
| CheckOnly | 检查配置/依赖及模型，不进入问答；可能启动依赖，不是完全无副作用 |

启动器输出阶段、端口、/workbench/与/docs地址；终端可提问，/token显示开发令牌，/quit退出。
令牌属本次启动桩，服务重启后重新连接，不写进报告或提交。
依赖失败停止，不静默切模型桩；-PullMissing才允许拉取缺失向量模型。
Real模式自动调用start-mineru.ps1启动或复用已安装的MinerU；-MinerURoot可指定D盘环境目录（默认D:\MinerU），
-NoMinerU仅跳过自动启动，不切换默认或已有文件的解析方案。Demo不启动MinerU；CheckOnly也会准备该依赖。
未安装时明确提示且不自动安装；已安装但模型/接口检查失败则停止启动，不静默回退。

未显式设置数据库时复用/启动本机PostgreSQL，默认55433端口。
显式设置 `KS_DATABASE_URL` 时，启动器先执行 `SELECT 1` 验证 PostgreSQL 连接，再检查模型和运行迁移。
数据目录优先已初始化的backend/.data/pgdata，否则本机应用数据目录；KS_LOCAL_DB_DIR可指定ASCII路径。
API/Worker退出不等于后台数据库停止，停止用：

```powershell
.\backend\.venv\Scripts\python.exe backend/scripts/dev_db.py --stop
```

**不要为排障使用dev_db.py --reset，它会删除整个库。** 数据库与原件目录成对备份/迁移，临时库不得共用持久原件卷。

### 手动部署与迁移

确认目标KS_DATABASE_URL/KS_STORAGE_ROOT，先备份并停止Worker、处理旧任务。
在backend目录执行，API和Worker分别在不同终端运行且使用同一配置：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn knowledge.entrypoints.api:app --host 127.0.0.1 --port 8010
.\.venv\Scripts\python.exe -m knowledge.entrypoints.worker
```

当前head为0011_document_events；从0001升级前不能留下未处理旧任务。
该节点汇合会话归档链与结构分块、全文索引链，任一分支数据库均可用同一命令升级。
不对未知默认库运行迁移，不改历史迁移，不原地回填已发布片段。

从 0008_document_events 升级：该版本已改名为 0011_document_events 并挂在 0010 之后，
曾按旧编号迁移过的库会因找不到 revision 而启动失败。处理顺序为：
先删除 ks_kb_changes、ks_document_changes、ks_job_created、ks_job_changes 四个触发器和
ks_notify_document_change() 函数，再把 alembic_version 置回 0007_merge_optimization，
执行 alembic upgrade head，最后跑 backfill_ragflow_terms.py --apply 补齐派生词项。

### 本次数据库回退与备份

2026-10-30已将本机knowledge库由0005事务降至0003，移除快照表和库描述列；
8张核心业务表记录数与原件保持不变。保留最新上传和片段，不等于恢复历史时间点的数据内容。
备份先恢复到独立副本并试降级，验证业务表/列和记录数后才改原库。
备份在忽略目录 `backend/.data/rollback-backups/20261030T021547Z/`：before.dump是完整custom格式备份，
manifest.json记录版本和计数，迁移源码副本用于恢复演练。含业务资料，仅本地保存，禁止提交/外发。
恢复时先用pg_restore到新库验证，再评审切换；不要直接覆盖当前库，也不重复运行一次性降级脚本。

## 3. 同模型重建与人工恢复

1. 保存上传回执document_id/version_id/job_id，queued/running时等待，不重复发请求。
2. failed后先修模型/Worker/解析环境；加密或空原件应更正后上传新版本。
3. 读取最新generation，使用**最新上传回执version_id**，不是仍可能指向旧发布的current_version_id。
4. POST /api/v1/documents/{id}/reindex，UUID与代次换成实际值：

```json
{"version_id":"最新上传版本UUID","expected_generation":3}
```

202后查新job，409刷新详情与已有任务，不盲目重发。重建复用原件/版本和冻结配置，旧发布继续服务直到成功切换。
不支持在线换向量、历史回滚或表格/语义策略升级；原样重传去重不能代替恢复。

## 4. 已移除的DeepDoc与Docker依赖

2026-11-06按用户要求移除DeepDoc解析功能；一键启动不再检查、启动或依赖Docker Desktop。
历史DeepDoc构建只读保留，不再新增；新上传只支持基础解析或MinerU。
MinerU为本机独立Python环境，不通过容器运行，启动方式见§5。

## 5. 验证

### 解析器选择与MinerU

上传区选择“沿用服务默认 / 基础解析 / MinerU”，该选择也用于上传新版本。
API在multipart中传parser_profile；选项与冻结语义见设计“显式可选解析器”。PDF默认MinerU，可配KS_PDF_PARSER_PROFILE；其他格式默认可配KS_DOCUMENT_PARSER_PROFILE，
两端须重启到同一版本。同字节去重提示实际已有方案，不把选择当作已切换。
TXT/MD直接读取并告警，不依赖OCR服务；基础模式没有图片OCR，选基础上传图片仍报不支持。
MinerU结果ZIP读取上限`KS_MINERU_OUTPUT_BYTES`（默认64MB，含未读取的图片）；
正文`structured_content.json`仍受`parse_output_bytes`（默认16MB）限制，图片等未读取成员不计入。
避免大文档因整包体积被误判为解析失败。

MinerU固定本机4.1.2独立环境，主环境不安装其重依赖。用户已批准本次在D盘安装与下载basic模型；
后续启动默认MINERU_MODEL_SOURCE=local，缺模型明确失败，不自动联网下载。
准备环境时先将TEMP/TMP、PIP_CACHE_DIR指向D:\MinerU\tmp和D:\MinerU\cache\pip，
用已有Python创建D:\MinerU\venv，再用该环境pip安装mineru==4.1.2；不使用系统pip或主后端环境。
按官方[同步文件API](https://opendatalab.github.io/MinerU/usage/file_parse/)部署，项目根目录提供：

```powershell
.\start-mineru.ps1 -DownloadModels -CheckOnly # 仅显式下载并验证，不启动服务
.\start-mineru.ps1                          # 本地模型检查，启动8000/8003两个回环服务
.\start.cmd                                # 自动准备已安装的MinerU，再启动项目
```

启动脚本将环境/模型/缓存/临时文件/状态/日志放D:\MinerU，服务在后台隐藏窗口运行，打印PID与就绪进度；
日志位于D:\MinerU\logs，退出工作台不自动停止解析服务。复用前校验接口，不停止占用端口的其他进程。
启动脚本不改写KS_MINERU_URL；默认对应8003，若显式配置其他端点，仍以该配置为准。
确认API未被配置为外部Router/SaaS；wrapper本身不加载模型，不代表上游不会转发。
主项目配置KS_MINERU_URL=http://127.0.0.1:8003，可选KS_MINERU_TOKEN供本机认证代理，
KS_MINERU_TIMEOUT_SECONDS为总等待预算（默认330秒），而非性能承诺。代理必须是本机origin；不接外网地址或任意响应URL。
原件请求使用basic/auto/all，Word内嵌图片的二次请求强制OCR；超时或异常明确失败，不回退基础解析。
上游可能在超时/断连后继续工作，本项目不自动重提同次HTTP请求；须按本机服务的保留/清理规则处理其缓存。
4.1 ZIP要求structured_content.json与完整标记，限制压缩/解压预算，不解压任意路径或获取外链。
上传 DOC 可沿用默认方案；DOCX 若需识别图片，请在上传或更新前选 MinerU。识别内容可在“分块”中查看，按提示核对原图。
当前客户端以页/块定位，不承诺字框、Office物理分页或完整表格语义，相关warnings需人工核对。
实际OCR/版面效果验收独立于协议测试，当前已验证范围与遗留问题见根目录issues。
上游[输出契约](https://opendatalab.github.io/MinerU/reference/output_files/)及[许可](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)须按部署版本核对。

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/check.py
node --test frontend/core.test.mjs frontend/events.test.mjs frontend/documents.test.mjs frontend/browse.test.mjs
.\backend\.venv\Scripts\python.exe -u backend/scripts/browser_smoke.py
```

后端按预检、lint、类型、独立数据库回归/迁移/HTTP验收执行，不调用真实模型、不修改业务库。
跳过/xfail/排除用例不能算完整通过。非Windows中文PDF测试需KS_TEST_FONT_PATH指定已有字体。
浏览器检查仅在相关前端改动收尾运行，需要已有Node/测试浏览器，不自动安装。
CI定义在.github/workflows/backend.yml，文件存在不等于远端已实跑。

### 分块与检索升级

代码统一使用 `ragflow-v1`。API 和 Worker 使用同一配置、本地资源目录及向量模型。新文档默认按文件类型选解析器，正文按标题或句界约 512 tokens 分块，默认零重叠。

1. 停止接收上传，排空旧 Worker 的处理任务后关闭旧进程。保留数据库和配套原件卷。
2. 按本手册升级数据库。清理部署配置中的旧检索、RRF、重排和 OpenSearch 选项，将显式的 `KS_RETRIEVAL_PROFILE` 更新为 `ragflow-v1`。
3. 执行 `python backend/scripts/prepare_ragflow_resources.py --data-dir backend/.data/ragflow-resources`，一次准备并校验 cl100k_base、WordNet 和 huqie。工作台启动器默认读取项目内该目录，并将绝对路径传给 API、Worker；无需手动设置环境变量。直接启动 API、Worker 或回填时，仍需将 `NLTK_DATA` 设置为该目录的绝对路径，`TIKTOKEN_CACHE_DIR` 设置为其 `tiktoken` 子目录。显式的 `NLTK_DATA`、`TIKTOKEN_CACHE_DIR` 或 `DATA_GYM_CACHE_DIR` 优先，启动器不会覆盖。已有文件可用 `--wordnet-archive`、`--infinity-sdk-archive` 和 `--tiktoken-vocabulary` 离线准备；运行服务时不会下载或切换模型。
4. 显式设置目标 `KS_DATABASE_URL`，执行 `python backend/scripts/backfill_ragflow_terms.py --apply`，再不带 `--apply` 检查缺失数为 0。回填仅更新派生词项，保留分块、向量及发布指针。
5. 启动新 API 与 Worker。工作台启动器先检查本地词表，再在迁移后只读检查旧策略任务和词项缺口。缺失词项只输出警告，不阻止工作台启动；连接后页面显示当前用户受影响的片段数及「重新检查」，修复后提示消失。相关范围的关键词检索与问答仍明确报索引不可用，不当成无结果；文件管理及未受影响范围可继续使用。若仍有旧策略排队/运行任务，API照常启动，Worker暂不启动，须按第1步处理后重启；历史资料需要重新分块时上传新文件版本。数据库连接或迁移失败仍须修复后启动。

本轮按用户要求跳过真实模型效果验收。旧结果仅作为对应历史快照的记录。旧策略对照应在隔离的历史代码版本运行。


## 6. 排障

| 现象 | 先检查 |
| --- | --- |
| 页面无法访问 | 启动器实际地址、安全端口；占用换-Port，不擅自杀其他进程 |
| 401/404 | 本次令牌、资源归属、删除状态；UUID不是凭证 |
| running停滞 | Worker、stage/attempt、租约、模型等待，不手改任务状态 |
| 模型故障 | 端点、认证、余额、名称、1024维及索引模型兼容性，不把故障记成无资料 |
| KEYWORD_INDEX_UNAVAILABLE | 检查本地词典及 `backfill_ragflow_terms.py` 的缺失分块数。 |
| Ollama绑定失败/10013 | 检查Windows保留端口：`netsh interface ipv4 show excludedportrange protocol=tcp`；不要删系统规则。若11434被保留，将backend/.env的KS_EMBEDDING_BASE_URL改为经检查可用的本机端口（如http://127.0.0.1:21434/v1）。启动器为serve/pull同步设置OLLAMA_HOST，不改系统环境，不更换模型或下载资料；手动启动时也需在该终端设置同一OLLAMA_HOST。 |
| PARSE_FAILED | 格式/加密/资源限制、MinerU网络/接口/模型，不循环重传相同内容 |
| no_evidence | 范围、发布状态、阈值及原文是否支持，不用降低门槛制造答案 |
| 409 | 刷新文档/任务，不重复旧代次 |
| 原文404但分块在 | 数据库与持久原件卷是否配对，是否曾误用临时目录 |

按request_id关联请求，任务再看job_id/build_id、阶段与耗时。日志不加正文或密钥，外发前脱敏。
生产硬配额与身份接入另配置，不把采样资源保护当安全沙箱。

### 工作台实时连接

升级至 0008 后，API 为资料页提供 /api/v1/events。原有 REST 接口继续使用。
反向代理应关闭该路径的响应缓冲，空闲超时应大于 45 秒，允许 text/event-stream 长连接。
PostgreSQL 连接地址应支持会话级 LISTEN，事务池模式会破坏订阅。
每个可见资料页维持一个订阅，退出身份、切换到其他功能或隐藏页面时关闭。
KS_EVENT_STREAM_LIMIT 设置每个 API 进程的 SSE 上限，默认 8，范围 1–16，超限返回 503 并退避重试。
断线提示会保留旧列表，重连成功后自动重读快照，用户也可手动刷新。
通知不持久化，不以心跳代表任务进度。Windows 使用线程池等待数据库 socket，取消后最多一个等待周期释放连接。
