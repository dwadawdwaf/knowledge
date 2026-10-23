# 知识检索与智能问答助手：文档索引

_二创项目 · v0.3 · 2026-10-23_

---

## 四份文档

| 文档 | 回答什么问题 | 什么时候看 |
| --- | --- | --- |
| [design.md](design.md) | 系统是什么：范围、选型、表结构、流程、接口、接入方式 | 动手前通读；写代码时对照 |
| [decisions.md](decisions.md) | 为什么这样定、被否的方案、什么条件下要重审 | 改动选型或数据模型前翻对应条目 |
| [runbook.md](runbook.md) | 怎么起服务、怎么查任务、出问题怎么处置 | 上手第一天；排障时 |
| [background.md](background.md) | 为什么走这条路、现成平台能借鉴什么 | 评审与选型讨论时 |

**冲突时以实现规格为准**：`design.md` > 其他三份。术语只在 `design.md` 附录 A 定义，其他文档沿用。

项目路线图：[roadmap.png](roadmap.png)

## 维护规则

一个事实只在一处定义，其余引用。改错地方比不写更贵。

| 变更 | 同步到 |
| --- | --- |
| 新增或改名领域概念 | design.md 附录 A，并清理代码中的旧称 |
| 改动选型、表结构、不变量 | decisions.md 新增一条，含「重新评估条件」 |
| 改动启动入口、迁移方式、故障处置 | runbook.md |
| 接口字段或错误码变化 | 生成的 OpenAPI + 面向调用方的变更记录 |

## 当前状态

`backend/` 的 M1 已实现并经真实链路验证（embedded-postgres，PostgreSQL 18.6 + pgvector）：

- 8 张表的初始迁移、13 个接口、任务领取/租约/心跳/崩溃恢复、解析 → 分段 → 向量化 → 原子发布。
- 验收用例 M01–M08 全部有自动化集成测试（`backend/tests/integration/`），另含孤儿对象回收用例。
- Worker 启动扫描回收创建超过 1 小时且无引用的对象（design §5.1）。
- 结构化日志（单行 JSON）：`request_id` / `job_id` / `build_id` 贯通，埋点含任务排队时长、各阶段耗时、重试、过期租约回收、无证据与引用校验失败。
- lint（ruff）+ 类型检查（mypy）+ 全量测试通过：`cd backend && make lint && make typecheck && pytest -q`。

剩余（M2，接入形态依赖 [decisions.md](decisions.md) 0008 确认）：MCP 协议实现（stdio/SSE）与 `integrations/qwenpaw/` Skill 包；工具面（6 个）已在 `knowledge/mcp/tools.py` 固定。

未配置模型端点时使用本地确定性实现（HashEmbedder / StubGenerator），链路可跑通，但不代表检索与生成质量。
