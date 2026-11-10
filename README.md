# 知识检索与智能问答助手

主系统内的个人知识服务：文档上传、异步处理、检索、问答、原文引用、更新与删除。
技术栈为FastAPI、PostgreSQL + pgvector、数据库任务表及独立Worker；原件保存于文件目录。
浏览器工作台通过API同源提供，不引入通用AI平台或独立前端构建服务。

## 启动

首次安装见[运行手册](docs/runbook.md#1-安装与配置)。已有开发环境后，在根目录执行：

```powershell
.\start.cmd
```

- 默认真实模型模式，读取backend/.env，可能向配置的模型服务发送内容。
- 默认复用本机持久数据库和原件目录，退出后资料保留。
- 终端显示工作台地址；可直接输入问题查看答案与引用，`/token` 查看开发令牌，`/quit` 退出。
- 离线演示用 `.\start.cmd -Mode Demo`，使用临时库和模型桩，不代表模型效果。
- 常用参数：`-NoBrowser`、`-Port 8011`、`-CheckOnly`；高级解析服务准备见运行手册。

## 当前范围

基础支持TXT、MD、CSV、文本PDF、DOCX、XLSX、PPTX；旧版DOC、扫描PDF、图片及Word内嵌图片OCR使用本机MinerU。
支持混合检索、证据约束问答、对话存档、版本发布、同模型重建和删除阻断。
统一使用文档单元优先分块；可选加权召回、本机重排和 OpenSearch BM25。配置见[运行手册](docs/runbook.md#本机批量重切与可选检索策略)。
上传/新版本可显式选基础解析或MinerU；服务准备与精度边界见运行手册，纯文本不伪装OCR。
MinerU已准备D盘独立环境时，start.cmd自动启动或复用服务；默认离线，不自动下载模型，详见运行手册。
不支持旧版XLS/PPT、表格聚合问答、通用回答兜底、语义目录规划、共享授权、账号体系或token流式输出。
手动检索位于工作台“检索”入口和知识库的“检索”页签。
历史会话仅供展示，不参与问答上下文。真实模型效果和复杂格式识别尚未全面验收。

## 验证

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/check.py
node --test frontend/core.test.mjs
```

后端完整入口包括依赖预检、lint、类型检查、隔离库、迁移和HTTP验收，不调用真实模型。
普通pytest可能跳过数据库用例，不能代替完整检查。

## 文档

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码代理约束与完成标准 |
| [设计](docs/design.md) | 唯一实现规格，接口与核心不变量 |
| [运行手册](docs/runbook.md) | 安装、启动、迁移、恢复和验证 |
| [决策摘要](docs/decisions.md) | 关键取舍与重审条件 |
| [当前问题](issues.md) | 仍有效的未完成事项及验收边界 |

业务代码在backend/knowledge，测试在backend/tests，工作台在frontend。
不得提交密钥或真实资料；规格与实现冲突先核对并回报，不通过改文档悄悄改变已发布契约。
