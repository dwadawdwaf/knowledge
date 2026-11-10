# 实现规格

功能基线：14000c1。迁移操作见[运行手册](runbook.md)，取舍见[决策摘要](decisions.md)，未决项见[issues](../issues.md)。
本文不重新引入已回退的表格聚合、语义规划或分层通用回答。字段和DDL完整定义分别维护在OpenAPI、表定义与迁移中。

## 1. 定位与范围

主系统内的个人知识服务：建库、文档上传、异步处理、混合检索、证据问答、引用、版本更新/重建/删除。
工作台同源托管在/workbench/，提供文件、知识库、检索、问答与原件/提取正文浏览；不做Office保真编辑。
支持TXT/MD/CSV、文本PDF、DOCX、XLSX、PPTX；扫描件与图片须选MinerU。
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
省略时按检测出的实际类型选择服务默认：PDF使用MinerU（KS_PDF_PARSER_PROFILE），其他格式使用KS_DOCUMENT_PARSER_PROFILE。
显式parser_profile优先；所有已支持文档均可选择两种方案；
基础解析本来不具备图片OCR，选用时仍明确报不支持，不伪造正文。TXT/MD在两种方案中均直接读取文本并明确标记，不声称经过OCR。
MinerU接本机4.1 file-parse API，除TXT/MD外均提交原件，固定tier=basic、ocr_mode=auto、page_range=all，
从默认ZIP内structured_content.json读取全量结果，不安装其SDK/模型到主环境，不使用云端。
只允许回环origin，无代理/重定向，生成无业务文件名的上传名；不跟随响应中的URL，202仅按本次request_id构造本机轮询/下载路径，限制总等待/响应/解压预算。
schema/页码/完整性校验失败报PARSE_FAILED，缺少精确几何或Office物理页码时降级block/page并告警；外部HTML仅转纯文本，不执行或抓取图片。
MinerU输出正文中的NUL等长替换为U+FFFD，块及build标记mineru_nul_replaced；原件不改，块内码点偏移不缩短。
此为数据库文本兼容修复，不自动重建历史内容；恢复失败文档仍经显式重建创建新build。
参数随新build冻结，重试/普通重建保留，不改数据库或旧文档；同字节去重回原回执，并返回已有build的parser_profile，界面提示选择未生效，不借重传换策略。
界面上传及新版本明确选择；默认与两端配置需一致，高级服务故障不回退基础解析。
文档详情的parser_profile优先显示已发布方案，尚未首次发布时显示待构建方案，避免失败文件隐藏原选择。
解析失败仍使用PARSE_FAILED；服务未启动、令牌缺失/不匹配、版本不匹配仅返回固定白名单操作提示，不透传上游异常。
MinerU结果ZIP读取上限为KS_MINERU_OUTPUT_BYTES，正文JSON仍受parse_output_bytes限制。
工作台只有显式选择历史会话才高亮该历史项；点击对话/文件等页面入口清除历史高亮，不删除会话内容。

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
