# 知识检索与智能问答助手

把内部资料变成**可检索、可问答、可追溯引用**的小型知识库，作为主系统的一个服务模块运行。
不是通用 AI 平台：不做账号体系、不做前端富编辑、不做多租户运营后台。

| 维度 | 现状 |
| --- | --- |
| 服务 | Python 3.12 / FastAPI |
| 存储 | PostgreSQL 18 + pgvector（向量固定 1024 维）+ 本地对象目录 |
| 异步 | 数据库持久任务表 + 独立 Worker（不使用 Celery） |
| 模型 | 向量 `bge-m3`、生成 `deepseek-flash`；任何 OpenAI 兼容端点均可替换 |
| 界面 | `/workbench/` 浏览器工作台：对话、知识库、全部文件、检索测试（排布参考 Yuxi） |

## 能力

- **导入** —— 支持 Markdown / TXT / PDF / DOCX；白名单外格式直接 415，加密与空白文件给确定性错误码。
- **处理** —— 解析 → 分段 → 向量化 → 原子发布成新 build；失败可重试，重复上传按字节去重。
- **检索** —— 关键词（`pg_trgm`）与语义（`pgvector`）双路召回，RRF 融合后截断。
- **文件浏览** —— 「全部文件」里打开任意文件：PDF 内嵌原页、图片直显、其他格式下载原件；切到「提取正文」按分段与页码阅读，支持页内查找。
- **问答** —— 只依据命中的证据作答，引用键由服务端分配并校验；证据不足返回 `no_evidence`，不编造。
- **分块视图** —— 文档行点「分块」可看到该版本的分段结果：按字符区间等比例画出覆盖条，横向重叠即共享内容，并列出每块的定位与原文。
- **生命周期** —— 新版本处理期间旧版本仍可读、发布后立即切换；处理中删除不会复活；Worker 被杀后租约过期被回收续跑。

## 快速开始

前置：Windows + 已建好的虚拟环境 `backend\.venv`（首次安装见 [backend/README.md](backend/README.md)）。

```powershell
.\start.cmd
```

这一条命令按顺序完成：检查 Python 与依赖 → 校验模型配置 → **启动依赖**（本机 Ollama 未运行则拉起，
并确认模型存在、维度为 1024）→ 选择空闲端口 → 起临时 PostgreSQL、API 与 Worker → 导入自制资料 →
打开工作台。终端输入 `/token`，把令牌粘到页面即可操作；也可以直接在终端提问。

| 参数 | 作用 |
| --- | --- |
| （无） | 真实模型 + 自制资料，默认入口 |
| `-Mode Demo` | 完全离线：先跑 10 步验收，再用模型桩问答 |
| `-CheckOnly` | 只检查环境、启动依赖并自检模型，不进入问答 |
| `-PullMissing` | 本机缺少向量模型时自动拉取（约 1.2 GB） |
| `-Port 8011` | 指定端口；默认 0 表示自动选空闲端口 |
| `-NoBrowser` | 不自动打开浏览器 |
| `-NoDeepDoc` | 本次运行不用 DeepDoc（PDF/图片退回旧解析器，扫描件不可用） |

每次运行都使用**临时数据库与临时资料卷**，不触碰现有数据，退出即清理。
模型配置读取 `backend/.env`（不入库）；密钥不会打印。
依赖失败时脚本停止启动并给出具体原因（未启动 / 鉴权 / 余额 / 模型名 / 维度不符），**不会静默回退到模型桩**。

## 目录

```
backend/
  knowledge/
    entrypoints/   api / worker / mcp 三个进程入口，只做装配
    api/           路由、请求校验、响应组装、异常映射
    services/      用例编排与事务边界
    domain/        实体、分块算法、端口、错误（不依赖框架）
    repositories/  SQL 与表定义
    adapters/      解析、向量、生成、存储、对外出口
    worker/        任务领取、租约、心跳
  migrations/      Alembic 迁移
  tests/           unit / integration / e2e
  testdata/        自制资料与评测语料
  scripts/         本地库、依赖、演示、评测、检查入口
frontend/          浏览器工作台（由 API 以 /workbench/ 提供）
docs/              设计、决策、运行手册、依据与进度
start.cmd / .ps1   一键启动
AGENTS.md          编码规范（改代码前必读）
```

依赖方向：`entrypoints → {api, mcp, worker} → services → domain ← repositories / adapters`。

## 文档

| 文档 | 回答什么问题 |
| --- | --- |
| [docs/design.md](docs/design.md) | 系统是什么：范围、表结构、流程、接口、不变量。**与其他文档冲突时以它为准** |
| [docs/decisions.md](docs/decisions.md) | 为什么这样定、被否的方案、何时需要重审 |
| [docs/runbook.md](docs/runbook.md) | 怎么起服务、怎么排障、怎么重建与恢复 |
| [docs/background.md](docs/background.md) | 选型依据与平台参考 |
| [docs/next-stage.md](docs/next-stage.md) | 当前进度与下一阶段顺序 |
| [docs/n3-evaluation.md](docs/n3-evaluation.md) | 固定问题集的真实效果评测与已知缺口 |
| [docs/README.md](docs/README.md) | 文档索引与维护规则 |

## 验证

| 目的 | 命令 |
| --- | --- |
| 单元测试 | `cd backend && pytest -q` —— 当前 122 通过，59 项需要独立测试库而跳过 |
| 完整交付检查 | `python -u backend/scripts/check.py` —— 锁定版本、lint、类型检查、隔离库 + HTTP E2E |
| 前端 | `cd backend && make frontend-test`；浏览器冒烟 `make browser-test` |
| 效果评测 | `python -u backend/scripts/evaluate_local.py` —— 默认离线，真实评测需显式授权 |

## 现状与边界

**已实现**：8 张表、14 个接口；验收用例 M01–M08 均有集成用例；版本发布、崩溃恢复、删除阻断由真实数据库验证。

**未完成**：MCP 协议（6 个工具面已定，协议未实现）；N3 效果验收（同主题无答案仍返回 502，不认定通过）；
主系统接入与真实资料接入。

**不含**：部署与运维编排、账号体系、审计与配额；仓库内只允许自制或经授权的语料。

未配置模型端点时会退化为 `HashEmbedder` / `StubGenerator`，仅用于验证链路形状，**不代表检索与生成质量**。
换向量模型后必须重建索引并重新标定阈值。

## 约定

- 改代码前先读 [AGENTS.md](AGENTS.md)，它定义了分层、命名与不可违反的硬约束。
- 不提交密钥、真实资料与过大的二进制样本。
- 文档与实现冲突时以实现为准，并回报冲突；一个事实只在一处定义。
