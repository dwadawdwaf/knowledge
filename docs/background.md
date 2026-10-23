# 选型依据与平台参考

_二创项目 · v0.3 · 2026-10-23_

> 解释为什么选这条路、现成平台能借鉴什么。系统行为见 [design.md](design.md)，关键取舍见 [decisions.md](decisions.md)。
> 功能条目依据各官方仓库 README 于 2026-10-23 抓取；标注「工程判断」的是本项目取舍，不是厂商声明。

---

## 1. 路线结论

| 维度 | 自有 API 接现成平台 | 轻量自研后端 |
| --- | --- | --- |
| 验证基本问答 | 较快 | 需实现最小链路 |
| 主系统权限融合 | 需建映射与隔离 | 规则直接可控 |
| 文档版本与删除语义 | 需验证平台契约 | 按本项目要求定义 |
| 复杂文档能力 | 取决于平台 | 依赖解析组件适配 |
| 长期开发责任 | 集成、兼容、平台升级 | 处理链路、评测、故障恢复 |

**选择**：业务契约自主掌握，算法组件可以替换。

- 自主掌握：知识库与文档标识、身份授权、版本发布、任务状态、检索与引用格式。
- 复用组件：文档解析、向量化、重排、生成模型，不重写成熟能力。
- 允许切换：用同一套样本对照现成平台；若平台更合适，通过适配层接入，不要求调用方改业务。

**改变路线的触发条件**：甲方主要需要可视化流程编排 → 以平台为主；大量扫描件与复杂表格导致解析失败 → 引入专门解析服务；主项目已有可复用知识服务 → 优先集成。

## 2. 为什么不用平台上的一切

两家平台都在向 Agent 平台膨胀（工作流画布、知识编译、Agentic 检索）。本项目要守住的恰是它们不擅长的三件事：**授权粒度、版本与删除语义、引用可校验**。这三个后补代价极高，所以在第一版就必须自建。

## 3. FastGPT

### 官方声明的能力

| 分组 | 能力 |
| --- | --- |
| 应用编排 | Agent Skill 编排；对话与插件工作流（含 RPA 节点）；双向 MCP |
| 应用调试 | 知识库单点搜索测试；对话中引用可反馈、修改、删除；完整链路日志；应用评测 |
| 知识库 | 多库复用混用；chunk 可改删；QA 拆分导入；TXT/MD/HTML/PDF/DOCX/PPTX/CSV/XLSX；URL 读取；混合检索与重排；API 知识库 |
| 运营 | 免登录分享；iframe 嵌入；对话记录统一查阅 |

### 值得借鉴

| 参考点 | 借鉴层次 | 落到本项目 |
| --- | --- | --- |
| 知识库单点搜索测试 | 交互范式 | 开发期必须有 `/search` 调试入口，并列展示两路排名与融合排名。没有它，调参就是盲调 |
| 完整调用链路日志 | 工程实践 | `request_id / job_id / build_id / model_revision` 贯通 |
| 多库复用 | 数据模型 | 请求带 `knowledge_base_ids` 数组而非单库 |
| API 知识库 | 产品定位 | 知识库要能被外部 Agent 当工具调用，这是 MCP 适配层的依据 |

### 不适用

工作流画布与 Agent 编排（我们是被编排方）；QA 拆分导入（需 LLM 预生成问答对，与「以原文为证据」冲突）；免登录分享与 iframe（等于开匿名通道）；平台自带评测体系（投入大于首期全部工程量）。

### 许可

Apache-2.0 附加条款：**允许作为后台服务商用，不允许提供 SaaS 服务**，未授权时须保留版权信息。因此其代码不能嵌入对外服务，只能作对照基线。

## 4. RAGFlow

### 官方声明的能力

| 分组 | 能力 |
| --- | --- |
| 文档理解 | DeepDoc 深度文档理解；超大上下文下的精确检索 |
| 分块 | 模板化分块，智能且可解释，多模板可选 |
| 知识编译 | 文档与数据集两级编译，产出百科、图谱、树、页面索引、思维导图、时间线、技能 |
| 检索 | Agentic Retrieval：问题分解、多步检索、证据验证；Low/Medium/High/Ultra 四档思考深度 |
| 引用 | 分块可视化、人工介入、可溯源引证 |
| 数据源 | Word、PPT、Excel、TXT、图片、PDF、扫描件、结构化数据、网页 |
| 工作流 | 精简编排；可配置 LLM 与嵌入模型；多路召回 + 融合重排；API |

补充：开源 1.0 中 DeepDoc 的版面分析、OCR、表格识别走 CPU 推理。

### 值得借鉴

| 参考点 | 借鉴层次 | 落到本项目 |
| --- | --- | --- |
| 分块可视化 + 人工介入 | 交互范式 | 每个分块能反查原文位置；`GET /chunks/{id}` 足够支撑核对 |
| 模板化分块 | 设计思想 | 按格式选分段策略，把模板落成 `chunk_config` 字段 |
| 可溯源引用 | 契约 | 引用携带 document / version / build / chunk 与 locator |
| 多路召回 + 融合重排 | 算法分层 | 融合进第一版；重排留作可插拔的一步 |

### 不适用

知识编译（图谱、树、时间线）需要额外 LLM 编排与长耗时构建，与「可追溯原文」是两套体系；Agentic 多步检索让 token 与延迟不可控，且引用来源不稳定；完整 DeepDoc 链路依赖重、CPU 慢，缺少实测数据支撑。

### 许可

Apache-2.0；仍需分别核对依赖、模型与商业组件。

## 5. 直接依赖的组件

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| FastAPI | API 框架 | MIT |
| PostgreSQL + pgvector + pg_trgm | 元数据、向量、关键词 | PostgreSQL |
| pypdf / python-docx | PDF、DOCX 解析 | BSD / MIT |
| BGE-M3 | 向量化 | MIT |
| QwenPaw | Agent 宿主 | Apache-2.0 |

## 6. 尚不能下结论的部分

以下只能由实测回答，不作为设计承诺：

- 中文资料的解析准确率与分段质量（需格式挑战集）
- 关键词路与语义路的实际 Recall@K
- 引用语义支持率（格式校验通过不等于结论有据）
- 解析、检索、生成的端到端耗时
- 真实业务问题的命中率与无答案误判率

## 7. 来源

- FastGPT：[仓库](https://github.com/labring/FastGPT)（README 使用协议章节）、[许可](https://github.com/labring/FastGPT/blob/main/LICENSE)
- RAGFlow：[仓库](https://github.com/infiniflow/ragflow)（README Key Features 章节）
- Dify（未采用，仅记录）：[仓库](https://github.com/langgenius/dify)、[许可](https://github.com/langgenius/dify/blob/main/LICENSE)
- PostgreSQL：[全文检索](https://www.postgresql.org/docs/current/textsearch-controls.html)、[行锁与 SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html)
- pgvector：[仓库](https://github.com/pgvector/pgvector)
- FastAPI：[文档](https://fastapi.tiangolo.com/)
- Document AI 解析候选：[Docling](https://github.com/docling-project/docling)
- 模型：[BGE-M3](https://huggingface.co/BAAI/bge-m3)、[bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- QwenPaw：[仓库](https://github.com/agentscope-ai/QwenPaw)、[文档](https://qwenpaw.agentscope.io/)

上游主分支会变化，正式对照前应记录 release、commit 与镜像摘要。
