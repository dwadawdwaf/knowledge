# RAG 分块与检索基线报告

- 运行时间：2026-11-11 16:31:39
- 语料：96 份（中文 48 / 英文 48），查询 228 道（可答 208 / 无答案 20）
- 环境：Linux 6.8.0-138-generic / Python 3.12.15 / embedded PostgreSQL / real embedding endpoint
- 分块：ragflow-v1，目标 512 tokens，零重叠；生成器：本地桩（不评估生成质量）

## 1. 解析与分块

- 成功入库 96 份，失败 0 份，跳过 0 份（需要 MinerU）。
- MinerU OCR：14 份文档、54 个分块。
- 共 2715 个分块；长度中位数 86 字符，P95 316，最大 2838。
- 空分块 0，少于 50 字符 160，超过 4000 字符 0，重复分块 119。
- 章节/表头保留率均值：0.9875（仅统计成功入库文档）；未完全保留的文档 5 份。

分格式分块：

| 格式 | 文档 | 分块 | 每文档分块中位数 | 字符中位数 | 少于50字符 |
| --- | --- | --- | --- | --- | --- |
| csv | 12 | 972 | 81.0 | 79.5 | 10 |
| docx | 12 | 136 | 11.0 | 84.5 | 15 |
| md | 12 | 147 | 12.0 | 100.25 | 3 |
| pdf | 12 | 52 | 2.5 | 503.5 | 3 |
| png | 12 | 25 | 1.0 | 288.5 | 3 |
| pptx | 12 | 144 | 12.0 | 99.75 | 51 |
| txt | 12 | 27 | 2.0 | 1159.5 | 0 |
| xlsx | 12 | 1212 | 101.0 | 91.0 | 75 |

## 2. 检索指标

| 模式 | Hit@1 | Hit@5 | Hit@20 | 任意证据@5 | MRR | 无答案有候选 | 无答案最高分均值 | P50 ms | P95 ms | 错误 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| keyword | 0.5192 | 0.5529 | 0.6683 | 0.649 | 0.5447 | 20/20 | 0.0681 | 1441.2 | 11025.1 | 0 |
| semantic | 0.2452 | 0.5385 | 0.851 | 0.5913 | 0.3774 | 20/20 | 0.6185 | 509.8 | 1179.3 | 0 |
| hybrid | 0.7548 | 0.8173 | 0.9135 | 0.8702 | 0.7877 | 17/20 | 0.27 | 2208.0 | 11882.3 | 0 |

Hit@K 表示同一分块（跨节题则 Top-K 并集）完整包含全部金标准证据；任意证据@5 表示只命中部分证据。
延迟在 10 个并发客户端下测得，仅用于相对比较，不代表串行或生产环境的 P95。

## 3. 分维度

### 按 format

| 分组 | 题数 | keyword Hit@5 | semantic Hit@5 | hybrid Hit@5 |
| --- | --- | --- | --- | --- |
| csv | 24 | 0.3333 | 0.2083 | 0.2917 |
| docx | 24 | 0.5833 | 0.4583 | 1.0 |
| md | 50 | 0.8 | 0.725 | 0.85 |
| pdf | 24 | 0.5833 | 0.2917 | 0.9583 |
| png | 24 | 0.4583 | 0.5417 | 0.8333 |
| pptx | 24 | 0.5833 | 1.0 | 1.0 |
| txt | 24 | 0.5833 | 0.4583 | 1.0 |
| xlsx | 24 | 0.3333 | 0.5 | 0.5833 |

### 按 lang

| 分组 | 题数 | keyword Hit@5 | semantic Hit@5 | hybrid Hit@5 |
| --- | --- | --- | --- | --- |
| en | 111 | 0.4216 | 0.5392 | 0.8039 |
| zh | 117 | 0.6792 | 0.5377 | 0.8302 |

### 按 type

| 分组 | 题数 | keyword Hit@5 | semantic Hit@5 | hybrid Hit@5 |
| --- | --- | --- | --- | --- |
| cross_language | 8 | 0.125 | 0.875 | 0.25 |
| cross_section | 8 | 0.875 | 0.625 | 1.0 |
| lexical | 96 | 0.6667 | 0.4688 | 0.8438 |
| no_answer | 20 | None | None | None |
| paraphrase | 72 | 0.5417 | 0.6528 | 0.9583 |
| table_row | 24 | 0.1667 | 0.3333 | 0.4167 |

## 4. 主要失败与限制

- 跳过文档：0 份；MinerU 已覆盖 12 张 PNG 与 2 份双栏/扫描 PDF。
- PDF 解析：普通 PDF 使用本地 `legacy-v1`（pypdf）；双栏/扫描 PDF 显式使用 MinerU，二者分块粒度不同。
- 失败文档：0 份；详见 results.json 的 documents 字段。
- 查询错误：0 个（模式级）；按 429/5xx 自动重试后仍失败才计入。

未命中题目（按 hybrid，最多 20 条）：

- `csv-zh-01-lex` 资产台账中编号为 CC-CSV-ZH-01-R17 的资产，原值是多少元？（金标准：11069）
- `csv-zh-01-row` 资产台账中编号为 CC-CSV-ZH-01-R17 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-01-R17）
- `csv-zh-02-lex` 资产台账中编号为 CC-CSV-ZH-02-R18 的资产，原值是多少元？（金标准：11206）
- `csv-zh-02-row` 资产台账中编号为 CC-CSV-ZH-02-R18 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-02-R18）
- `csv-zh-03-lex` 资产台账中编号为 CC-CSV-ZH-03-R19 的资产，原值是多少元？（金标准：11343）
- `csv-zh-03-row` 资产台账中编号为 CC-CSV-ZH-03-R19 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-03-R19）
- `csv-zh-04-lex` 资产台账中编号为 CC-CSV-ZH-04-R20 的资产，原值是多少元？（金标准：11480）
- `csv-zh-04-row` 资产台账中编号为 CC-CSV-ZH-04-R20 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-04-R20）
- `csv-zh-05-lex` 资产台账中编号为 CC-CSV-ZH-05-R21 的资产，原值是多少元？（金标准：11617）
- `csv-zh-05-row` 资产台账中编号为 CC-CSV-ZH-05-R21 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-05-R21）
- `csv-zh-06-lex` 资产台账中编号为 CC-CSV-ZH-06-R22 的资产，原值是多少元？（金标准：11754）
- `csv-zh-06-row` 资产台账中编号为 CC-CSV-ZH-06-R22 的记录，责任人是谁、存放在哪里？（金标准：CC-CSV-ZH-06-R22）
- `csv-en-01-row` In the asset ledger, who is responsible for record CC-CSV-EN-01-R17, and where is it stored?（金标准：CC-CSV-EN-01-R17）
- `csv-en-03-lex` In the asset ledger, what is the original value of asset CC-CSV-EN-03-R19?（金标准：12165）
- `csv-en-03-row` In the asset ledger, who is responsible for record CC-CSV-EN-03-R19, and where is it stored?（金标准：CC-CSV-EN-03-R19）
- `csv-en-04-row` In the asset ledger, who is responsible for record CC-CSV-EN-04-R20, and where is it stored?（金标准：CC-CSV-EN-04-R20）
- `csv-en-06-row` In the asset ledger, who is responsible for record CC-CSV-EN-06-R22, and where is it stored?（金标准：CC-CSV-EN-06-R22）
- `xlsx-en-01-lex` In the asset ledger, what is the original value of asset CC-XLSX-EN-01-R17?（金标准：13535）
- `xlsx-en-01-row` In the asset ledger, who is responsible for record CC-XLSX-EN-01-R17, and where is it stored?（金标准：CC-XLSX-EN-01-R17）
- `xlsx-en-02-lex` In the asset ledger, what is the original value of asset CC-XLSX-EN-02-R18?（金标准：13672）

## 5. 复跑

```powershell
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/run_baseline.py
# 本机 MinerU 就绪后纳入图片与扫描件：
.\backend\.venv\Scripts\python.exe testdata/rag-baseline-v1/run_baseline.py --include-mineru
```

完整逐题排名、分块与逐文档结果见同目录 `results.json`、`summary.json`、`chunks.json`。