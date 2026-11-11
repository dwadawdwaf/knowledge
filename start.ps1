[CmdletBinding()]
param(
    [ValidateRange(0, 65535)]
    [int]$Port = 0,
    [ValidateSet('Real', 'Demo')]
    [string]$Mode = 'Real',
    [switch]$CheckOnly,
    [switch]$PullMissing,
    [switch]$NoBrowser,
    # 仅启动已安装的独立环境，不自动安装依赖或下载模型。
    [switch]$NoMinerU,
    [string]$MinerURoot = 'D:\MinerU'
)

# 真实问答或本地演示入口，复用已有启动器；数据库默认用本机内置实例（见 scripts/local_db.py）。
# 保留 UTF-8 BOM，以便 Windows PowerShell 5.1 正确读取中文提示。
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'
$entryName = if ($Mode -eq 'Real') { 'ask_local.py' } else { 'demo_local.py' }
$entry = Join-Path $PSScriptRoot "backend\scripts\$entryName"
$deps = Join-Path $PSScriptRoot 'backend\scripts\deps.py'
$previousConsoleEncoding = [Console]::OutputEncoding
$previousOutputEncoding = $OutputEncoding
$exitCode = 1

# Chromium 内核会直接拒绝连接这些端口（ERR_UNSAFE_PORT），
# 页面表现为“无法访问此网站 / 可能已永久移动到新网址”，而服务本身是正常的。
$unsafePorts = @(2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080)

function Test-LoopbackPortFree([int]$Candidate) {
    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Candidate)
    try {
        $probe.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        try { $probe.Stop() } catch { }
    }
}

function Select-SafePort {
    # 优先固定小段范围，便于记忆，也避开系统把动态端口分到浏览器保留端口。
    foreach ($candidate in 8010..8099) {
        if ($unsafePorts -contains $candidate) { continue }
        if (Test-LoopbackPortFree $candidate) { return $candidate }
    }
    for ($attempt = 0; $attempt -lt 32; $attempt++) {
        $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
        $probe.Start()
        $candidate = $probe.LocalEndpoint.Port
        $probe.Stop()
        if ($candidate -gt 1023 -and $unsafePorts -notcontains $candidate) { return $candidate }
    }
    throw '找不到浏览器可用的空闲端口，请用 -Port 8011 显式指定。'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = [Console]::OutputEncoding
    Write-Host '[1/5] 检查本地 Python 环境...' -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '缺少 backend\.venv\Scripts\python.exe，请先按 docs\runbook.md 安装开发环境；脚本不会自动下载依赖。'
    }
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw '缺少问答启动入口，请确认 backend\scripts 下的项目文件完整。'
    }
    $probe = @'
import importlib
import sys
if sys.version_info < (3, 11):
    print('需要 Python 3.11 或更高版本')
    raise SystemExit(1)
modules = ('fastapi', 'uvicorn', 'sqlalchemy', 'psycopg', 'alembic', 'pgvector',
           'httpx', 'embedded_postgres', 'psutil', 'pypdf', 'docx')
for name in modules:
    try:
        importlib.import_module(name)
    except ImportError:
        print('缺少或无法加载依赖: ' + name)
        raise SystemExit(1)
print('Python ' + sys.version.split()[0] + '：开发依赖检查通过')
'@
    & $python -X utf8 -c $probe
    if ($LASTEXITCODE -ne 0) {
        throw '环境检查失败，请按 docs\runbook.md 安装或修复开发依赖。'
    }

    Write-Host '[2/5] 检查模型模式与配置...' -ForegroundColor Cyan
    if ($Mode -eq 'Real') {
        & $python -X utf8 -u $entry --check
        if ($LASTEXITCODE -ne 0) {
            throw '启动预检未通过，请按上方提示检查模型配置或本地词表资源；不会自动切换到模型桩。'
        }
    }
    else {
        Write-Host '本地演示模式：不调用外部模型，也不读取真实模型密钥。'
    }
    Write-Host '[3/5] 启动依赖服务...' -ForegroundColor Cyan
    if ($Mode -eq 'Real') {
        if ($NoMinerU) {
            Write-Host '[MinerU] 已跳过自动启动；不修改已有文档的解析方案。' -ForegroundColor Yellow
        }
        elseif (Test-Path -LiteralPath (Join-Path $MinerURoot 'venv\Scripts\mineru-kit.exe') -PathType Leaf) {
            Write-Host '[依赖] 检查并启动D盘MinerU服务（不下载模型）...' -ForegroundColor Cyan
            & (Join-Path $PSScriptRoot 'start-mineru.ps1') -Root $MinerURoot
            Write-Host '[依赖] MinerU已就绪，继续检查数据库和模型。'
        }
        else {
            Write-Host "[MinerU] 未找到 $MinerURoot 下的独立环境，未自动安装；选择MinerU解析前请按运行手册准备。" -ForegroundColor Yellow
        }
        $depsArgs = @()
        if ($PullMissing) { $depsArgs += '--pull-missing' }
        & $python -X utf8 -u $deps $depsArgs
        if ($LASTEXITCODE -ne 0) {
            throw '依赖服务未就绪，请按上方提示处理后重试；不会回退到模型桩。'
        }
        $nativeEngine = & $python -X utf8 -c "import sys; sys.path.insert(0,'backend'); from scripts.real_model import _RealSettings; print(_RealSettings().search_engine)"
        if ($nativeEngine -eq 'opensearch') {
            try {
                & (Join-Path $PSScriptRoot 'start-opensearch.ps1')
            }
            catch {
                Write-Host '[OpenSearch] 未就绪；文件管理仍可启动，检索页面将提示索引状态。请检查本机搜索服务。' -ForegroundColor Yellow
            }
        }
        $env:KS_DEPS_READY = '1'
    }
    else {
        Write-Host '演示模式使用内置向量桩，无需启动外部服务。'
    }
    Write-Host '[4/5] 检查端口...' -ForegroundColor Cyan
    if ($Port -eq 0) {
        $Port = Select-SafePort
        Write-Host "自动选择端口：$Port"
    }
    if ($unsafePorts -contains $Port) {
        throw "端口 $Port 是浏览器保留端口（如 5060 = SIP），Chrome/Edge 会直接报 ERR_UNSAFE_PORT，服务虽已启动但页面打不开。请改用 .\start.cmd -Port 8011。"
    }
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
    }
    catch {
        throw "端口 $Port 已被占用或无法绑定。请使用 .\start.cmd -Port 8011，或 -Port 0 自动选择空闲端口；不会停止已有服务。"
    }
    finally {
        $listener.Stop()
    }
    Write-Host "本机地址：http://127.0.0.1:$Port/docs"

    if ($CheckOnly) {
        Write-Host '[5/5] 检查通过，依赖服务已就绪，未启动问答服务。' -ForegroundColor Green
        $exitCode = 0
    }
    else {
        Write-Host '[5/5] 启动问答服务，终端将持续输出执行进度...' -ForegroundColor Cyan
        Write-Host '启动本机数据库与 API/Worker；数据保存在本机，不访问外部数据库。'
        Write-Host '准备完成后自动打开浏览器工作台；在终端输入 /token 复制本次开发令牌到页面。'
        Write-Host '也可继续在终端提问。输入 /quit 或按 Ctrl+C 退出；-NoBrowser 可关闭自动打开。'
        $browserArgs = @()
        if ($NoBrowser) { $browserArgs += '--no-browser' }
        if ($Mode -eq 'Real') {
            Write-Host '真实模式：问题及证据会发送到已配置模型；问答正文不写入报告或服务日志。'
            & $python -X utf8 -u $entry --port $Port @browserArgs
        }
        else {
            Write-Host '演示模式：先执行 10 步验收，再进入本地模型桩问答。'
            & $python -X utf8 -u $entry --serve --port $Port @browserArgs
        }
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Host "启动器已退出，退出码：$exitCode。请查看上方错误及输出的日志路径。" -ForegroundColor Red
        }
    }
}
catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousOutputEncoding
    Remove-Item Env:\KS_DEPS_READY -ErrorAction SilentlyContinue
    Pop-Location
}
exit $exitCode
