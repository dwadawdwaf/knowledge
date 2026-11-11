# 在 Windows 主机上跑 rag-baseline-v1：OpenSearch 组件 vs PostgreSQL 组件。
#
# 背景：沙箱 VM 只能经网关访问主机，网关对所有端口都返回空响应，无法复用主机上
# 已运行的 OpenSearch(127.0.0.1:19276) 与 PostgreSQL(127.0.0.1:55433)。因此这份
# 基线必须在主机上执行。脚本只做三件事：
#   1. 复用主机的 OpenSearch（未启动则调用 start-opensearch.ps1）；
#   2. 用独立 embedded PostgreSQL + 独立索引导入同一批 96 份语料；
#   3. 先测 OpenSearch，再用同一个库、同一批分块测 PostgreSQL，便于同机对比。
# 不写入业务索引：索引名固定为 ks-rag-baseline-os-v1，默认运行前会删除该测试索引
# 以及上一次的测试数据目录（加 -Resume 则保留并续跑）。

param(
    [string]$Index = "ks-rag-baseline-os-v1",
    [string]$DataRoot = "backend/.data/rag-baseline-os",
    [switch]$SkipOcr,
    [switch]$SkipPostgresCompare,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$Backend = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Repo = (Resolve-Path (Join-Path $Backend "..")).Path
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if ([System.IO.Path]::IsPathRooted($DataRoot)) { $Base = $DataRoot } else { $Base = Join-Path $Repo $DataRoot }
$PgData = Join-Path $Base "pg"
$OutOs = Join-Path $Base "out-os"
$OutPg = Join-Path $Base "out-pg"

if (-not (Test-Path $Python)) { throw "找不到项目虚拟环境：$Python" }
New-Item -ItemType Directory -Force -Path $Base | Out-Null

function Test-Http([string]$Url) {
    try {
        $null = Invoke-RestMethod -Uri $Url -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(1500)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

Write-Host "[1/4] 检查主机 OpenSearch (127.0.0.1:19276) ..."
if (-not (Test-Http "http://127.0.0.1:19276/")) {
    Write-Host "       未运行，调用 start-opensearch.ps1"
    & (Join-Path $Repo "start-opensearch.ps1")
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline -and -not (Test-Http "http://127.0.0.1:19276/")) {
        Start-Sleep -Seconds 3
    }
}
if (-not (Test-Http "http://127.0.0.1:19276/")) { throw "OpenSearch 在 180 秒内未就绪" }
$health = Invoke-RestMethod -Uri "http://127.0.0.1:19276/" -TimeoutSec 5
Write-Host ("       OpenSearch {0} {1}" -f $health.version.distribution, $health.version.number)

if ($Resume) {
    Write-Host "[2/4] -Resume：保留测试索引与数据目录，续跑未完成的文档与查询"
} else {
    Write-Host "[2/4] 清理上一次的测试索引与数据目录 ..."
    try {
        Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:19276/$Index" -TimeoutSec 10 | Out-Null
        Write-Host "       已删除测试索引 $Index"
    } catch {
        Write-Host "       测试索引不存在"
    }
    foreach ($path in @($OutOs, $OutPg, $PgData)) {
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}

$env:KS_SEARCH_ENGINE = "opensearch"
$env:KS_OPENSEARCH_URL = "http://127.0.0.1:19276"
$env:KS_OPENSEARCH_INDEX = $Index

$ocr = (-not $SkipOcr) -and (Test-Port 8000) -and (Test-Port 8003)
if ($ocr) {
    Write-Host "[3/4] 检测到 MinerU 8000/8003，本次纳入 14 份 OCR 文档"
} else {
    Write-Host "[3/4] 未检测到 MinerU 8000/8003，本次跳过 14 份 OCR 文档（需完整口径请先启动 MinerU）"
}

$common = @(
    (Join-Path $PSScriptRoot "run_baseline.py"),
    "--pg-data", $PgData,
    "--workers", "10",
    "--pdf-parser", "legacy"
)
if ($ocr) { $common += "--include-mineru" }

Write-Host "[4/4] OpenSearch 检索基线（导入 + 228 题 × 3 模式）..."
$started = Get-Date
& $Python @common --out $OutOs --search-engine opensearch --opensearch-url $env:KS_OPENSEARCH_URL --opensearch-index $Index
if ($LASTEXITCODE -ne 0) { throw "OpenSearch 基线运行失败，退出码 $LASTEXITCODE" }
Write-Host ("       OpenSearch 完成，用时 {0:N1} 分钟" -f ((Get-Date) - $started).TotalMinutes)

if (-not $SkipPostgresCompare) {
    Write-Host "     同机 PostgreSQL 对照（复用同一库与分块，仅重跑查询）..."
    New-Item -ItemType Directory -Force -Path $OutPg | Out-Null
    foreach ($name in @("state.json", "chunks.json", "documents.json")) {
        Copy-Item (Join-Path $OutOs $name) (Join-Path $OutPg $name) -Force
    }
    & $Python @common --out $OutPg --queries-only --search-engine postgresql
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL 对照运行失败，退出码 $LASTEXITCODE" }
}

Write-Host ""
Write-Host "完成。结果目录："
Write-Host "  OpenSearch : $OutOs"
if (-not $SkipPostgresCompare) { Write-Host "  PostgreSQL : $OutPg" }
Write-Host "每个目录含 summary.json、results.json、report.md。"
