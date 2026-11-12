# RAG 分块与检索基线语料 v1

用于本项目中英文 RAG 的分块、召回与排序回归。全部内容是虚构示例（公司、产品、人员、编号均不存在），
不含真实单位资料，遵循仓库硬约束 C2。语料冻结后不按检索结果修改题目。

## 1. 规模与构成

| 项目 | 数量 |
| --- | --- |
| 文档总数 | 96（中文 48 / 英文 48） |
| 每种格式 | 12（中文 6 / 英文 6，格式内覆盖制度、技术文档、产品 FAQ） |
| 查询总数 | 228（可答 208 / 同主题无答案 20） |
| 全部可入库 | 96（启动 MinerU 后；12 张 PNG、1 份双栏 PDF、1 份扫描 PDF 走 OCR） |
| 主题分布 | 制度 32 / 技术 32 / 产品 FAQ 32 |

格式：TXT、Markdown、DOCX、CSV、XLSX、PPTX、PDF、PNG。

## 2. 目录结构

```
rag-baseline-v1/
├── corpus/
│   ├── md/    txt/    docx/    csv/
│   ├── xlsx/  pptx/   pdf/     png/
├── generate.py          # 确定性生成 corpus 与 manifest.json
├── manifest.json        # 文档、查询、金标准、SHA-256
├── queries.jsonl        # 查询集平铺版，便于接入其他评测脚本
├── validate.py          # 校验冻结哈希、计数与金标准可定位性
├── run_baseline.py      # 隔离库导入 + 检索指标 + 报告
├── report.md            # 最近一次基线报告
└── baseline-20261111.md / .json
```

文件名格式为 `<lang>-<序号>-<主题>.<扩展名>`，例如 `corpus/md/zh-01-travel-expense.md`。

## 3. 刻意覆盖的分块与检索难点

| 格式 | 主要特性 |
| --- | --- |
| Markdown | 多级标题、跳级标题、重复“附录”、代码围栏内的伪标题/伪表格、管道表格、长列表、front matter |
| TXT | 无标题纯文本、Q&A 块、超长单段、CRLF、UTF-8 BOM |
| DOCX | Heading 1–3、表格、合并表头/合并数据单元格、项目符号、分页符、长段落 |
| CSV | 80 行台账、带逗号与换行的引号字段、行尾制表符、列数不一致的记录、BOM |
| XLSX | 多工作表、合并标题、合并数据单元格、日期/数字、90 行数据（超过 40 行分块阈值） |
| PPTX | 12 页、标题/要点、表格页、章节分隔页、演讲者备注、超长要点 |
| PDF | 多页单栏文本、表格、跨页段落；1 份双栏与 1 份扫描件需 MinerU |
| PNG | 扫描通知、表格截图、监控图表、设置界面截图、长扫描件，全部需 MinerU OCR |

语料还包含同主题中英文平行文档、近似编号的辅助记录、同主题但事实不存在的无答案题，用于测试跨语言召回与误召回。

## 4. 查询集设计

| 类型 | 数量 | 说明 |
| --- | --- | --- |
| lexical | 96 | 用文档编号 + 属性直接问事实，偏关键词 |
| paraphrase | 72 | 用唯一别名和自然问法，不出现原值，偏语义 |
| table_row | 24 | CSV/XLSX 台账按资产编号问责任人与位置，要求同一行分块 |
| cross_section | 8 | 同一文档两个不同章节的事实，要求 Top-K 并集覆盖 |
| cross_language | 8 | 中文文档用英文问、英文文档用中文问，检验多语言向量 |
| no_answer | 20 | 10 道同文档不存在事实，10 道完全不存在实体 |

金标准是“文档 + 原文中必须出现的片段（needles）”，不绑定具体分块 ID，因此重新分块后仍可复用。
匹配时做 NFKC、转小写、去空白；`same_chunk=true` 表示同一分块必须包含全部 needles，否则按 Top-K 并集计算。

`manifest.json` 主要字段：

- `documents[]`：`id`、`file`、`format`、`lang`、`theme`、`title`、`code`、`alias`、`features`、`requires_mineru`、`check_terms`、`facts`、`gold_row`、`sha256`
- `queries[]`：`id`、`query`、`lang`、`doc_lang`、`format`、`theme`、`type`、`difficulty`、`answerable`、`doc`、`gold{needles, section, value, same_chunk}`

## 5. 基线结果（2026-11-11）

环境：Linux sandbox / Python 3.12 / embedded PostgreSQL / BGE-M3（BAAI/bge-m3，1024 维，SiliconFlow）；
分块 `ragflow-v1`，目标 512 tokens、零重叠；生成器为本地桩，不评估问答生成质量。
MinerU 4.1.2（basic、ONNX、CPU）在本机 8000/8003 启动；12 张 PNG 与 2 份双栏/扫描 PDF 走 MinerU，
其余 PDF 显式使用本地 `legacy-v1`（pypdf）。检索在 10 个并发客户端下测得，延迟只用于相对比较。

导入 96 份文档、2715 个分块；0 份失败、0 份跳过；执行 228 道查询（可答 208、无答案 20），684 次检索，查询错误 0。

| 模式 | Hit@1 | Hit@5 | Hit@20 | 任意证据@5 | MRR | 无答案有候选 | 无答案最高分均值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| keyword | 0.5192 | 0.5529 | 0.6683 | 0.6490 | 0.5447 | 20/20 | 0.0681 |
| semantic | 0.2452 | 0.5385 | 0.8510 | 0.5913 | 0.3774 | 20/20 | 0.6185 |
| hybrid | **0.7548** | **0.8173** | **0.9135** | **0.8702** | **0.7877** | 17/20 | 0.2700 |

分格式 hybrid Hit@5：TXT 1.000、DOCX 1.000、PPTX 1.000、PDF 0.958、MD 0.850、PNG 0.833、XLSX 0.583、CSV 0.292。
分语言 hybrid Hit@5：中文 0.830、英文 0.804；keyword 中文 0.679、英文 0.422。
分类型 hybrid Hit@5：paraphrase 0.958、cross_section 1.000、lexical 0.844、table_row 0.417、cross_language 0.250。

分块：长度中位数 86 字符、P95 316、最大 2838；少于 50 字符 160 块；重复文本 119 块；
成功入库文档的章节/表头保留率均值 0.9875（5 份文档缺少标题或 1 个 PDF 章节标题）。
CSV/XLSX 为行级分块（中位数 80–91 字符），TXT/PDF 为整页或整段级分块（中位数约 1100 字符）。

### OCR / MinerU 执行情况

- 本报告在 Linux sandbox 内单独安装 `mineru==4.1.2` 与 basic ONNX 模型，并启动 8000/8003 两个本机服务；
  Windows 开发机按 `docs/runbook.md` 使用 `D:\MinerU`，无需在主后端环境安装 MinerU 依赖。
- 12 张 PNG 与 2 份特殊 PDF 全部成功：MinerU 4.1.2、basic tier、ONNX CPU，共 54 个 OCR 分块；
  单页 PNG 约 12 秒，双栏/扫描 PDF 导入约 25–30 秒（含解析与向量化）。
- PNG hybrid Hit@5 20/24；剩余 4 道全部是表格截图（`png-zh-02`、`png-en-02`）的文档级问题。
  OCR 文本已完整包含编号和值，失败原因与 CSV/XLSX 的表格行检索相同：编号/高频词稀释，而非 OCR 漏字。
- 解析告警均显式保留：`mineru_geometry_unavailable`、`mineru_requires_review`（全部 OCR 文档），
  `mineru_table_linearized`（表格截图与特殊 PDF），`mineru_content_skipped`（监控图表）。
  这些告警不阻断入库，但引用定位只到页/块，不能承诺字框。
- 修复过程中发现并解决了两类非服务问题：FAQ 事实中的换行导致图片文字重叠并被截断；
  表格截图列宽不足与英文按字符断行导致 OCR 把编号/单词拆开。修复后重新生成 PNG 并回填。

### 主要观察

1. **混合检索整体最好。** 总体 Hit@5 0.817、MRR 0.788；paraphrase 0.958、lexical 0.844。
   加入 OCR 文档后总体与仅文本基线（Hit@5 0.811）基本持平并略高，尽管新增的 28 道 OCR 查询更难；
   现在 8 种格式都有真实结果，不再用“跳过”掩盖图片能力。
2. **OCR 服务可用，剩余问题在检索而非识别。** 14/14 份 OCR 文档入库；20/24 道 PNG 查询命中。
   表格截图的两道题仍受“编号+高频词”稀释影响，说明 OCR 文本进入索引后仍需要表格行级检索优化。
3. **跨语言仍是融合短板。** cross_language hybrid Hit@5 0.250，纯语义 0.875。
   关键词分支权重 0.7、向量 0.3，跨语言时关键词候选弱，语义候选被融合挤出；需要语言感知权重或语义优先。
4. **表格行精确查找最弱。** table_row hybrid Hit@5 0.417、keyword 仅 0.167，CSV 格式整体 0.292。
   查询中的“资产台账/责任人/存放位置”等高频词稀释了编号词项；需要行级/单元格级索引、业务编号加权或结构化查询解析。
5. **无答案区分不足。** 20 道无答案题在语义模式下全部有候选，最高分均值 0.619；hybrid 仍有 17/20 有候选。
   仅靠余弦或融合分数阈值难以区分“相关但无答案”，应增加证据覆盖/答案可判定性检查。
6. **分块长度呈双峰。** CSV/XLSX 行级块过短（160 块少于 50 字符，缺少表头上下文），TXT/PDF 又整页合并
   （普通 PDF 每份约 2 块，MinerU 特殊 PDF 14–15 块），不利于定位和重排。建议按格式配置分块策略。
7. **英文关键词弱于中文。** keyword Hit@5：中文 0.679、英文 0.422；需要检查英文分词、词干与业务编号保留。

### 限制

- MinerU 在 CPU/ONNX basic 上运行，延迟和精度不代表 GPU 或 standard tier；未做多轮重复与方差估计。
- MinerU 不提供字框，表格被线性化；OCR 文档的引用定位只到页/块，`mineru_requires_review` 需人工抽查。
- 普通 PDF 仍走本地 pypdf，特殊 PDF 走 MinerU；两种解析路径的分块粒度不同（约 2 块 vs 14–15 块），不能直接横向比较。
- 语料为自制虚构内容，绝对数值不能与其他数据集横向比较。
- 生成器为本地桩，未评估答案生成、引用忠实度与幻觉。
- 延迟受 10 个并发客户端与外部 embedding 服务影响，不代表生产 P95。

## 6. 复现

生成/校验语料：

```powershell
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/generate.py
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/validate.py
```

完整基线（本报告口径：临时库、MinerU 已启动、普通 PDF 走本地 pypdf）：

```powershell
.\start-mineru.ps1
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/run_baseline.py --include-mineru --pdf-parser legacy
```

不使用 MinerU 时，14 份 OCR 文档会被跳过：

```powershell
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/run_baseline.py
```

让默认 `pdf-auto-v1` 决定哪些 PDF 交给 MinerU（带表格的 PDF 会走 OCR）：

```powershell
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/run_baseline.py --include-mineru --pdf-parser auto
```

结果写入 `backend/.data/rag-baseline-20261111/`：`results.json`（逐题排名）、`summary.json`、`chunks.json`、`report.md`。

### 对比检索组件（OpenSearch / PostgreSQL）

检索组件由 `KS_SEARCH_ENGINE` 切换。沙箱 VM 无法访问 Windows 回环地址（网关对所有端口都返回空响应），
因此复用主机已运行的 OpenSearch 时必须在主机上执行；索引固定为测试专用名，不写业务索引：

```powershell
.\run-opensearch-baseline.cmd          # 或 .\backend\testdata\rag-baseline-v1\run_opensearch_host.ps1
```

脚本会复用（或启动）`127.0.0.1:19276` 的 OpenSearch，删除并重建 `ks-rag-baseline-os-v1`，
用独立 embedded PostgreSQL 导入同一批语料，先跑 OpenSearch，再复用同一库与分块跑 PostgreSQL 对照。
检测到 MinerU 8000/8003 时纳入 14 份 OCR 文档，未检测到时自动跳过（可加 `-SkipOcr` 显式跳过）。
默认每次重建测试索引与数据目录；中断后用 `-Resume` 续跑，`-SkipPostgresCompare` 只测新组件。

结果位于 `backend/.data/rag-baseline-os/out-os` 与 `out-pg`，对比：

```powershell
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/compare.py `
  backend/.data/rag-baseline-os/out-pg/summary.json `
  backend/.data/rag-baseline-os/out-os/summary.json --labels PostgreSQL,OpenSearch `
  --md backend/.data/rag-baseline-os/compare.md
```

`run_baseline.py` 也支持单独指定 `--search-engine`、`--opensearch-url`、`--opensearch-index`，
便于对已有持久库做 `--queries-only` 重测。


在单次命令约 3 分钟的执行环境里，可用持久库分阶段导入与断点续跑（`--pg-data` 必须位于支持 initdb 的本地磁盘）；
OCR 批次运行期间需保持 MinerU 8000/8003 服务在同一个命令会话内启动：

```bash
python run_baseline.py --pg-data /tmp/ragpg --out /path/out --doc-start 0 --doc-count 36 --import-only --pdf-parser legacy
python run_baseline.py --pg-data /tmp/ragpg --out /path/out --doc-start 84 --doc-count 4 --import-only --include-mineru --replace --pdf-parser legacy
python run_baseline.py --pg-data /tmp/ragpg --out /path/out --queries-only --include-mineru --pdf-parser legacy
```

修改分块/检索后，用同一份 `manifest.json` 重跑，对比总体与 `format/lang/type` 分片；
不要根据结果修改冻结题目。新增/替换语料时提升 `version` 并保留旧版本。
