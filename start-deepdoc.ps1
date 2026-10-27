# DeepDoc 独立服务入口；保留 BOM 兼容 Windows PowerShell 5.1 的中文输出。
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$LaunchWorkbench
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    Write-Host '[1/4] 检查 Docker Compose，不自动安装系统软件...'
    # 新安装后的终端可能尚未刷新 PATH；兼容本项目约定的 D 盘安装位置。
    $dockerBin = 'D:\Docker\Desktop\resources\bin'
    if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and
        (Test-Path -LiteralPath (Join-Path $dockerBin 'docker.exe') -PathType Leaf)) {
        $env:PATH = $dockerBin + ';' + $env:PATH
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '未找到 Docker。请先安装并启动支持 Linux 容器的 Docker Desktop，再重试。'
    }
    & docker compose version
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose 不可用。' }
    $osType = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎未运行。' }
    if ($osType -ne 'linux') { throw '请将 Docker 切换为 Linux 容器模式。' }
    Write-Host 'Linux 容器引擎已就绪。'
    if ($CheckOnly) {
        Write-Host '预检结束；未下载镜像、未启动容器、未验证模型。'
        return
    }
    if (-not $env:DEEPDOC_TOKEN) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $env:DEEPDOC_TOKEN = [Convert]::ToBase64String($bytes)
    }
    if ($env:DEEPDOC_TOKEN.Length -lt 32) { throw 'DEEPDOC_TOKEN 至少需要 32 字符。' }
    Write-Host '[2/4] 构建固定版本镜像；首次下载体积较大，构建输出实时显示...'
    & docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml --progress plain build
    if ($LASTEXITCODE -ne 0) { throw '镜像构建失败；不会切回旧解析器。' }
    Write-Host '[3/4] 启动本机解析服务...'
    & docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml up -d
    if ($LASTEXITCODE -ne 0) { throw '容器启动失败。' }
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:9381/health' -TimeoutSec 2
            if ($health.profile -eq 'deepdoc-v0.22.0-v1') { $ready = $true; break }
        }
        catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw '服务未就绪，请查看 docker compose 的服务日志。' }
    $env:KS_DOCUMENT_PARSER_PROFILE = 'deepdoc-v0.22.0-v1'
    $env:KS_DEEPDOC_URL = 'http://127.0.0.1:9381'
    $env:KS_DEEPDOC_TOKEN = $env:DEEPDOC_TOKEN
    Write-Host '[4/4] HTTP 服务已启动；尚未验证实际 OCR/表格效果。令牌未输出、未写入文件。'
    Write-Host '本 PowerShell 会话已配置后端。可运行 .\start.cmd；或使用 -LaunchWorkbench 连续启动。'
    Write-Host '服务不会随工作台退出停止；停止方式见 docs/runbook.md 第14节。'
    if ($LaunchWorkbench) { & (Join-Path $PSScriptRoot 'start.ps1') }
}
catch {
    Write-Host "DeepDoc 启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally { Pop-Location }
