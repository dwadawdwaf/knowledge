# 实现规格

功能基线：14000c1。迁移操作见[运行手册](runbook.md)，取舍见[决策摘要](decisions.md)，未决项见[issues](../issues.md)。
本文不重新引入已回退的表格聚合、语义规划或分层通用回答。字段和DDL完整定义分别维护在OpenAPI、表定义与迁移中。

## 1. 定位与范围

主系统内的个人知识服务：建库、文档上传、异步处理、混合检索、证据问答、引用、版本更新/重建/删除。
工作台同源托管在/workbench/，提供文件、知识库、检索、问答与原件/提取正文浏览；不做Office保真编辑。
支持TXT/MD/CSV、PDF、DOC/DOCX、XLSX、PPTX；DOC、扫描件与图片OCR须使用MinerU。
不做账号体系、共享授权、网页连接器、通用Agent平台、复杂表格自动理解或token流式输出。

## 2. 技术选型

优先复用成熟组件：FastAPI/Pydantic、PostgreSQL、pgvector、pg_trgm；任务与业务同库，独立Worker领取，不引入Celery。
原件放文件目录。当前向量索引固定1024维，同一物理索引不混写不同模型或维度，换模型须离线迁移和重标阈值。
基础解析复用pypdf、python-docx、openpyxl与标准库CSV/OOXML，不支持旧版XLS/PPT。

### 2.1 已移除的DeepDoc

2026-11-06按用户要求移除DeepDoc解析与Docker依赖，保留MinerU；历史DeepDoc构建只读保留，不再新增。
新上传/新版本只接受`legacy-v1`与`mineru-api4.1-v1`，接口与冻结语义见“显式可选解析器”。

## 3. 架构与代码

### 显式可选解析器（本次增量）

上传及新版本multipart新增可选parser_profile：legacy-v1、mineru-api4.1-v1；
省略时按检测类型选默认：PDF使用KS_PDF_PARSER_PROFILE，旧版DOC只支持MinerU，其余格式使用KS_DOCUMENT_PARSER_PROFILE。
显式parser_profile优先，不支持该格式的方案明确拒绝；DOC要求`.doc`扩展名及基本OLE容器头校验，文件内容由MinerU校验。
基础解析本来不具备图片OCR，选用时仍明确报不支持，不伪造正文。TXT/MD在两种方案中均直接读取文本并明确标记，不声称经过OCR。
MinerU接本机4.1 file-parse API，除TXT/MD外均提交原件，固定tier=basic、ocr_mode=auto、page_range=all，
从默认ZIP内structured_content.json读取全量结果，不安装其SDK/模型到主环境，不使用云端。
只允许回环origin，无代理/重定向，生成无业务文件名的上传名；不跟随响应中的URL，202仅按本次request_id构造本机轮询/下载路径，限制总等待/响应/解压预算。
schema/页码/完整性校验失败报PARSE_FAILED，缺少精确几何或Office物理页码时降级block/page并告警；外部HTML仅转纯文本，不执行或抓取外链。
DOC/DOCX保留原生正文；引用到同一结果ZIP中`images/`的PNG/JPEG/TIFF素材再提交同一个本机MinerU接口（`ocr_mode=ocr`）。原件与外链不为OCR另行读取；不解压到磁盘。原件与图片请求共享总等待预算，单文档最多读取32次素材，累计素材解压字节受KS_MINERU_OUTPUT_BYTES限制。仅把识别出的文本作为独立块，标记`word_image_ocr_requires_review`，不冒充原生文字或物理页码。缺失、跳过或无识别文字记录warnings；服务故障不以部分正文冒充成功。DOCX基础解析发现内嵌素材但仅提取正文时标记`docx_images_require_mineru`。
MinerU输出正文中的NUL等长替换为U+FFFD，块及build标记mineru_nul_replaced；原件不改，块内码点偏移不缩短。
此为数据库文本兼容修复，不自动重建历史内容；恢复失败文档仍经显式重建创建新build。
解析参数随新build冻结，重试/普通重建保留；曾用已移除DeepDoc的TXT/MD重切时改为基础直读文本并在新构建告警，其他历史格式不静默改解析器。同字节去重回原回执，并返回已有build的parser_profile，界面提示选择未生效，不借重传换策略。
界面上传及新版本明确选择；默认与两端配置需一致，高级服务故障不回退基础解析。
文档详情的parser_profile优先显示已发布方案，尚未首次发布时显示待构建方案，避免失败文件隐藏原选择。
解析失败仍使用PARSE_FAILED；服务未启动、令牌缺失/不匹配、版本不匹配仅返回固定白名单操作提示，不透传上游异常。
MinerU结果ZIP读取上限为KS_MINERU_OUTPUT_BYTES，正文JSON仍受parse_output_bytes限制。
工作台只有显式选择历史会话才高亮该历史项；点击对话/文件等页面入口清除历史高亮，不删除会话内容。

### 工作台实时同步

`GET /api/v1/events` 使用与 REST 相同的 Authorization。单向 SSE 发送 `sync`、`change`、`heartbeat`、`expired` 事件，data 固定为 `{"version":1}`，不传正文、资源路径或令牌。连接先订阅，再发 sync，使客户端重读权威 REST 快照。事件是失效信号，不保证逐条回放。

PostgreSQL 迁移 `0008_document_events` 为知识库、文档和任务状态建立事务通知。通道从已验证 Actor 的 subject_id 推导，与现有 owner 授权一致。任务续租不通知。每条 SSE 使用独立 LISTEN 连接，15 秒无变更发送心跳，退出或断线关闭连接，不持有业务事务。数据库需支持会话级 LISTEN，连接池的事务模式不能代替该连接。

每个 API 进程最多接受 KS_EVENT_STREAM_LIMIT 条资料连接，默认 8，配置范围 1–16。超限返回可重试 503，普通 REST 保持可用。该上限用于资源保护，不代表已经验证的部署容量。

资料页面保持一条 SSE，隐藏、离开资料页、退出身份时取消。重连采用 1–30 秒退避，每次重连重读快照，45 秒无流量终止旧连接。变更合并后刷新，列表没有变化不重绘。失去实时连接显示提示，手动刷新始终可用。问答仍在证据与引用校验完成后一次性返回。

切换资料页面只重读快照，不重建连接：订阅只跟随身份与可见性，避免每次切页都取消一条流并重新 LISTEN。渲染阶段发出的读取同样不因切页被取消，写回由身份代次与所属视图的归属校验决定。

运行单元：API、Worker、PostgreSQL、原件目录，MinerU按需启用。
依赖方向：`entrypoints → {api, mcp, worker} → services → domain ← repositories / adapters`。
API/MCP/Worker并列不互相依赖；services编排事务，repositories只访问数据库，adapters访问文件、模型与解析器。
domain不依赖框架。命名、类型、日志与验证纪律唯一见根目录AGENTS，不在此重复。

## 4. 数据模型

八张业务表，UUID主键，时间UTC，JSON用JSONB。完整字段/索引/约束见backend/knowledge/repositories/tables.py及migrations。

| 表 | 核心语义 |
| --- | --- |
| knowledge_bases | owner来自Actor；状态active/deleting/deleted，archived表示保留数据但隐藏（不进列表，也不能作为检索或上传范围） |
| documents | current_version_id是已发布版本，active_build_id是当前构建，pending_build_id是待处理构建；generation为更新/删除栅栏 |
| document_versions | 不可变上传，保存version_no、object_key、sha256、media_type、byte_size；唯一(document_id,version_no)/(kb_id,sha256) |
| index_builds | 一次索引处理，冻结chunk_config及向量模型/维度；queued/running/ready/failed/cancelled/retired |
| chunks | build所属片段，保存ordinal、text、locator、warnings、embedding和关键词文本；唯一(build_id,ordinal)，向量HNSW/余弦和关键词GIN/trigram索引 |
| jobs | ingest/reindex/cleanup；queued/running/succeeded/failed/cancelled；处理任务固定build_id/generation，cleanup不填 |
| conversations | owner所属的对话；状态active/archived，archived保留消息但不进历史列表 |
| messages | 本轮问题/答案、状态、引用与模型标识，仅作历史展示，不参与后续生成上下文 |

locator页码1起，字符偏移为Unicode码点左闭右开；无法精确定位明确降级block/page。
DOCX用paragraph，不伪造分页；CSV/XLSX用sheet和真实1起行区间（两端包含），跳空行不压缩原坐标。
解析器的块内偏移不当作文档级偏移，跨页位置不裁成假的单页。警告按块保存，build保存并集。

### 不变量

1. 所有查询SQL内授权，无权、不存在、删除统一404，不先全库Top-K再过滤。
2. 仅active库、active文档、ready且等于active_build_id的内容可检索。
3. 发布在同事务校验有效lease_token、running、未过期租约、generation和pending_build_id。
4. 已发布内容不可改写；重试/重建产生新build，旧build退役不修改片段。
5. 引用仅指向本轮送模证据，excerpt由服务端从原文截取，模型不产生资源ID。
6. 删除提交即阻断访问；清理失败不恢复访问。持久库和临时库不共用原件卷。

## 5. 生命周期

上传：验证身份/格式/大小 → 服务端对象键写原件 → 单事务创建version/build/job并递增generation → 返回202回执。
同库同字节去重返回200；对象写入后事务失败的孤儿按超时规则回收，不在请求中误删其他库文件。

Worker在短事务用FOR UPDATE SKIP LOCKED领取任务，提交后解析/调用模型，独立线程续租。
默认租约120秒、心跳30秒、最多3次尝试，配置经Settings；启动及每轮领取前回收过期租约。
重试保留job ID、累计attempt和generation，新建同版本build并切换pending；旧代次取消，不接管新上传。
确定性格式/密码/空正文错误直接失败，可重试故障退避；心跳不复活过期租约。

发布按document → job → build加锁，单事务写片段、校验栅栏、切换发布指针、旧构建退役及任务成功。
失败不部分发布。首次发布前无可检索内容，更新期间旧发布继续服务。

删除事务置deleting、递增generation、清空指针、取消处理任务并建cleanup，立即不可检索/预览/读引用。
异步擦除原件、片段、构建和版本后置deleted，删除库先阻断整库。

## 6. 检索与问答

### 6.1 检索

query最多2000码点；knowledge_base_ids必填1–20个；top_k默认5、最多20；mode为hybrid/keyword/semantic。
两路SQL内均授权、限定当前发布构建。关键词按pg_trgm相似度、语义按余弦距离过阈值召回，RRF用Σ1/(60+rank)融合去重。
RRF只是排名分，不能替代相关性阈值。哈希向量阈值不能直接用于真实模型。
向量配置不兼容报MODEL_UNAVAILABLE，不冒充no_evidence；显式keyword仍可独立使用。

### 6.1.2 可选检索优化

默认检索为baseline-v1；可由KS_RETRIEVAL_PROFILE=optimized-v2启用优化，不沿用旧效果基线。

- ParsedBlock保留kind、heading_level、table_id及解析器确实提供的metadata；子进程序列化必须保留这些字段。
- 仅排除解析器显式标注的页眉/页脚并告警running_matter_omitted；不按重复短语删除正文。文本段落识别兼容LF/CRLF及空白行，原始码点偏移不归一化。
- DOCX恢复段落/表格在原件中的顺序；PPTX不跨幻灯片合并。CSV/XLSX按行拆分，保存跳空行前的真实行号；首行候选表头进入检索上下文并标记table_header_candidate，不声称已正确识别所有表头。
- 跨页仅在相邻页、同节和未完句等线索兼容时连接，标记cross_page_continuation_candidate；未知关系保持独立，章节/条款边界阻断关联。多栏顺序、扫描识别及跨页续表结构不足时保留解析告警，不伪造几何或续表关系。
- 合并片段用locator.precision=composite及sources保存每段chunk_start/chunk_end与原始locator，顶层不提供虚构的连续字符偏移。单来源保留原定位并附来源映射；前端展示全部来源页。
- chunks.structure保存strategy、kind、section_path、unit_id、table_id、is_header及context_sources，旧行迁移默认空对象。text是原文，tsv与向量输入可附片段缺少的最近结构标签及候选表头；附加上下文不能充当引用原文。
- optimized-v2保留原问题，补充NFKC/空白归一化版本及有限显式并列子问句，保留否定、数值、单位和范围。编号/条号提取仅来自用户原问题；不读取历史会话、不扩大库范围、不自动调用模型改写。
- 关键词对照使用全文/局部trigram与编号精确包含，语义仍使用原模型；各候选SQL显式排序并稳定处理并列。按配置加权RRF，扩展问题取最佳候选分数，避免按改写次数累加。
- 局部trigram另用keyword_local_floor（候选默认0.3），全文仍用原similarity_floor；两者尺度不同，不直接共用阈值。
- 内部候选数与公开top_k分离。可选本机HTTP重排端点通过Reranker端口注入，固定输入候选预算；无端点时不重排，已配置但故障时返回模型错误，不静默回退。重排分数不作为事实支持概率。
- 问答按片段数/字符预算选证据，去除包含重复，预留有限关联槽。同单元邻块或同表候选表头须通过授权SQL读取，限定同一active build；旧索引没有结构时不猜测邻接。补充片段保留独立证据键和引用，生成后复核全部来源。

阈值、权重、长度预算和真实效果仍需另行验证；不引入表格聚合、视觉生成或自动解析器切换。

在 `optimized-v2` 下，`KS_EVIDENCE_COVERAGE` 和 `KS_STRUCTURED_CONTEXT` 是两个独立、默认关闭的开关。前者保留整句混合检索，只对具有共同对象的显式字段清单或可独立阅读的并列问句补充需求召回。原首位候选保持在首位，每项在限定候选中选一个直接片段，同片段可覆盖多项。条件归属不明确或原句无候选时保留原排序。

后者只改变送入问答的关联证据。先选择直接证据，再在剩余预算内按原始表头定位、相邻页续句或同原始块连续窗口补全。补全沿用同一当前构建的授权查询，片段单独引用。检索接口的直接结果不受上下文开关影响。现有结构缺失或关系无法核验时不补全。

实验配置 `KS_RETRIEVAL_PROFILE=fts-v1` 使用一条原查询。英文关键词召回由 PostgreSQL 英文词项并集和 `ts_rank_cd` 排序，GIN 表达式索引复用 `chunks.tsv` 文本。含汉字、编号或无有效词项的查询走同一条 SQL 检索路径中的既有关键词排序。语义召回、加权 RRF、授权、当前构建和引用校验沿用现有规则。

显式选择 `bm25-v1` 时启用本机倒排索引；分块策略独立选择。关键词路径用本机 OpenSearch 的英文分析器和 Lucene BM25（k1=1.2，b=0.75），语义路径沿用 PostgreSQL/pgvector，混合用加权 RRF（k=60，关键词 0.5、向量 1.0）。只用一条原始查询，问答从直接命中中按现有预算选证据。

PostgreSQL 先查出当前有权访问的已发布构建，OpenSearch 按这些构建过滤后取 Top K，再由 PostgreSQL 校验候选的权限与发布版本，提供原文及定位。缺失构建首次使用时由 PostgreSQL 的检索文本批量同步，写入完成才标记，支持重复同步。倒排索引禁用原文回读，文档删除提交后立即阻断检索，清理任务移除该文档的倒排记录。OpenSearch 不可用返回可重试的服务错误。

### 6.1.3 PDF 版面分块

PDF 在统一分块前恢复版面。原件、发布事务和授权条件沿用现有规则。

- 优先使用解析器提供的真实块框。MinerU 4.1 的正文仍从 `structured_content.json` 读取，`middle_json.json` 经协议、生产者、物理页号和页内块数量及类型顺序核对后提供归一化块框。坐标缺失、通栏块与栏内块重叠或无法可靠归类的版面保留页内解析顺序，标记 `pdf_layout_uncertain`。
- 单栏按正文段落切分。双栏在可靠栏间距内先左后右。存在通栏摘要、图表或标题的混排页先按通栏块分区，再恢复各区的阅读顺序。跨栏、跨页仅连接与解析顺序兼容的明确续文，或位置、章节和续句线索一致的相邻正文。复合定位保留每段页码、栏位和实际可用的块框，`pdf_bbox` 是原解析块框。
- 显式中文条款在同页同版面区域内合并条号、相邻正文和连续编号分点；版面未知时只沿同页解析顺序并保留告警。下一个条号或标题断开，不为了合并分点跨栏、跨页。超出分块长度时，后续片段仍将条号加入检索文本，条号不重复写入引用原文。
- 表格独立成块，超长时优先在原有行边界拆开。公式、图注、脚注和代码各自保留边界。OCR 正文沿用解析器输出并标记 `pdf_ocr_requires_review`，未识别出文字的视觉块只在有可用图注或版面边界时参与处理。

### 6.1.4 文档单元优先分块

`evidence-unit-v1` 是唯一的写入策略，冻结到所有新构建。历史构建不改写，尚未完成的旧策略任务在取得租约后替换为新构建。普通重建使用现行策略及原长度预算和向量配置；批量维护优先重建当前发布版本，未曾发布的重建最新上传版本。先按解析器的阅读顺序、标题层级和显式条款/列表组织文档单元，再在单元内按长度预算装入完整段落与列表项。

- 新节、独立表格或其他独立内容是边界。PDF 沿用版面恢复；版面不明时只在同页按显式节、列表或解析器续文线索合并并保留告警，不假设跨页、跨栏关系。文档内部的表格、代码、图注等不混入正文块。
- 短元素不切开；超长元素先按句、行等自然边界拆分，无法自然切开的超长正文才用有界字符窗口。常规片段间不复制重叠正文。
- 同一单元拆出的块共享 `unit_id`，标题路径沿用 `section_path`；原文与来源按已有 `locator.sources` 回溯，不生成摘要或伪造原文。检索、排序、关联证据与重建接口行为不变。

### 6.2 问答

检索 → 证据门槛 → 生成 → 引用校验 → 来源复核 → 返回/可选存档。
无命中或分数不足返回no_evidence，不调用生成。模型合法空sections也返回no_evidence。
格式为 `{"sections":[{"text":"...","evidence_keys":["E1"]}]}`，证据键由服务端分配。
未知键丢弃；全部非空段落无有效引用报CITATION_VALIDATION_FAILED，不返回正文；部分段落无键保留并显式告警，这是当前兼容行为，不代表已完成事实支持验证。
引用来源变化/删除报SOURCE_CHANGED；上游超时或故障与资料不足区分。
answered/no_evidence为主要状态，conflicting_evidence为保留状态，不代表已实现冲突判断。
conversation_id用于存档，本轮生成不读取历史答复；无通用回答兜底和表格聚合路径。

## 7. API

前缀/api/v1，Bearer令牌，snake_case JSON，时间ISO8601带时区；完整模型以运行实例/docs、/openapi.json为准。

| 方法与路径 | 用途/成功码 |
| --- | --- |
| POST/GET /knowledge-bases | 建库201/本人列表200 |
| DELETE /knowledge-bases/{id} | 阻断并异步清理202 |
| POST/GET /knowledge-bases/{id}/documents | 上传202（去重200）/列表200 |
| GET/DELETE /documents/{id} | 详情200/删除202 |
| POST /documents/{id}/versions | 新版本202 |
| POST /documents/{id}/reindex | 同模型重建202 |
| GET /documents/{id}/chunks、/chunks/{id} | 分块与定位200 |
| GET /documents/{id}/file | 原件字节200；位图可as=png转预览，多帧只首页，原件不变 |
| GET /documents/{id}/sheets、/documents/{id}/docx | 受限只读预览200，不代表完整解析结构 |
| GET /jobs/{id} | 阶段/尝试/错误200，无真实进度时不虚构百分比 |
| POST /search、/qa | 检索/问答200 |
| POST/GET /conversations、GET/DELETE /conversations/{id} | 新会话201/列表200/历史200；删除200（消息级联，他人与不存在404，不用204） |

文件响应private/no-store/nosniff，Office原件attachment，不伪装HTML。引用预览仍需授权。
成功包装 `{data,meta:{request_id}}`；错误 `{error:{code,message,retryable},meta:{request_id}}`。

| HTTP | code |
| --- | --- |
| 401/404 | UNAUTHENTICATED / RESOURCE_NOT_FOUND |
| 409 | VERSION_CONFLICT / SOURCE_CHANGED / STALE_JOB |
| 413/415/422 | FILE_TOO_LARGE / UNSUPPORTED_FORMAT / VALIDATION_ERROR |
| 502/503/504 | CITATION_VALIDATION_FAILED / MODEL_UNAVAILABLE / MODEL_TIMEOUT |

### 7.1 重建与人工恢复

请求仅 `{"version_id":"UUID","expected_generation":1}`，两项必填，代次为严格非负整数，不接受模型/路径/owner等额外字段。
只重建最新上传版本，包括失败版本；无权/删除/版本不归属文档统一404。
代次过期、非最新、已有queued/running处理任务或向量配置不一致返回409，不取消原任务。
同事务新建build/job并递增generation，回执含document_id/version_id/build_id/job_id/generation。
原件和上传版本不变，沿用冻结配置，成功前旧发布可读，失败不撤销旧内容。
重复旧请求409，调用方刷新详情/任务；不支持历史回滚、在线换向量或策略升级。

## 8. 安全

本地使用HMAC签名subject_id/tenant_id/exp令牌，属于开发桩，主系统接入必须替换验证器。
身份不接受请求体自述；对象键服务端生成，文件名不拼接存储路径。
解析在可终止子进程执行，限制耗时/CPU/RSS、解压条目/总量及输出；采样限制不等于OS硬沙箱。
模型输入是资料而非指令，无文件/SQL/工具权限；端点与外发范围由配置和授权控制。
错误白名单输出，日志不记问题、答案、片段、原件、内部响应或密钥。真实接入前补审计、保留期和配额。

## 9. QwenPaw边界

不fork/接入宿主主仓，MCP与REST共用services；当前仅有工具函数，协议与Skill包未完成。
subject_id只由可信Skill配置注入，不从对话/参数推断；ReMe记忆不能当引用证据，接入形态待确认。

## 10. 验收

M01上传到检索、M02引用定位、M03无证据不生成、M04跨用户隔离、M05版本原子切换、
M06崩溃恢复不重复、M07删除不复活、M08同字节去重及版本区分，都应有自动回归。
完整检查不得以跳过/xfail/排除用例算通过；工程与真实模型效果分开，待办与效果只在issues维护。

## 附录 A 术语

| 术语 | 定义 |
| --- | --- |
| document/version/build | 资料身份/不可变上传/一次索引构建，重建只新增build |
| current_version/active_build | 当前发布版本/构建，不一定是最新上传 |
| generation/lease_token | 文档更新栅栏/单次领取凭证，不能互相替代 |
| chunk/locator/excerpt | 片段/原始定位/服务端原文摘录 |
| evidence_key/citation | 本轮服务端证据键/绑定真实资源与定位的引用；有效引用不等于事实必被支持 |
| parser_profile | build冻结的解析策略，历史缺省legacy-v1，不是模型权重指纹 |
| RRF | 按名次融合，不是相关性或答案支持概率 |
