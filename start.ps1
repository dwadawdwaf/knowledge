[CmdletBinding()]
param(
    [ValidateRange(0, 65535)]
    [int]$Port = 0,
    [ValidateSet('Real', 'Demo')]
    [string]$Mode = 'Real',
    [switch]$CheckOnly,
    [switch]$PullMissing,
    [switch]$NoBrowser,
    # 显式不用 DeepDoc：PDF/图片退回旧解析器。默认按 .env 的 profile 走，不静默回退。
    [switch]$NoDeepDoc
)

# 真实问答或本地演示入口，复用已有启动器，不加载现有业务数据库。
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
        throw '缺少 backend\.venv\Scripts\python.exe，请先按 backend\README.md 安装开发环境；脚本不会自动下载依赖。'
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
        throw '环境检查失败，请按 backend\README.md 安装或修复开发依赖。'
    }

    Write-Host '[2/5] 检查模型模式与配置...' -ForegroundColor Cyan
    if ($Mode -eq 'Real') {
        & $python -X utf8 -u $entry --check
        if ($LASTEXITCODE -ne 0) {
            throw '真实模型配置未通过检查，请修改 backend\.env；不会自动切换到模型桩。'
        }
    }
    else {
        Write-Host '本地演示模式：不调用外部模型，也不读取真实模型密钥。'
    }
    if ($NoDeepDoc) {
        $env:KS_DOCUMENT_PARSER_PROFILE = 'legacy-v1'
        Write-Host '[注意] 指定了 -NoDeepDoc：本次运行 PDF/图片用旧解析器，扫描件与版面/表格能力不可用。' -ForegroundColor Yellow
    }
    Write-Host '[3/5] 启动依赖服务...' -ForegroundColor Cyan
    if ($Mode -eq 'Real') {
        $depsArgs = @()
        if ($PullMissing) { $depsArgs += '--pull-missing' }
        & $python -X utf8 -u $deps $depsArgs
        if ($LASTEXITCODE -ne 0) {
            throw '依赖服务未就绪，请按上方提示处理后重试；不会回退到模型桩。'
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
        Write-Host '自动启动临时数据库与 API/Worker，仅导入自制资料，不修改现有业务数据。'
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
