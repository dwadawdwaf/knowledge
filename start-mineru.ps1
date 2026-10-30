# MinerU独立环境须事先安装；默认只用本地模型，不自动下载或修改系统环境。
param(
    [string]$Root = 'D:\MinerU',
    [switch]$DownloadModels,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root)
if (-not $Root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'MinerU环境、缓存与模型必须放在D盘。'
}
$kit = Join-Path $Root 'venv\Scripts\mineru-kit.exe'
if (-not (Test-Path -LiteralPath $kit)) { throw '尚未安装独立MinerU环境，请按docs/runbook.md准备。' }
foreach ($name in @('home','tmp','cache','state','logs')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $name) | Out-Null
}
$env:MINERU_HOME = Join-Path $Root 'home'
$env:HF_HOME = Join-Path $Root 'cache\huggingface'
$env:MODELSCOPE_CACHE = Join-Path $Root 'cache\modelscope'
$env:TEMP = Join-Path $Root 'tmp'
$env:TMP = $env:TEMP
$env:MINERU_MODEL_SMALL_BACKEND = 'onnx'
$env:MINERU_MODEL_SOURCE = 'local'
$env:HF_HUB_OFFLINE = '1'
$env:MINERU_MODEL_VLM_SERVER_URL = ''
if ($DownloadModels) {
    Write-Host '[MinerU 1/3] 已显式允许下载basic模型，输出与缓存均在D盘...'
    $env:HF_HUB_OFFLINE = '0'
    & $kit models download --tier basic --small-backend onnx --source modelscope
    if ($LASTEXITCODE -ne 0) { throw '模型下载失败，不启动服务、不切换解析器。' }
    $env:HF_HUB_OFFLINE = '1'
}
Write-Host '[MinerU 1/3] 检查本地basic模型；缺失即停止，不自动下载...'
& $kit models verify --tier basic --small-backend onnx
if ($LASTEXITCODE -ne 0) { throw '本地模型不完整；需要下载时显式使用 -DownloadModels。' }
if ($CheckOnly) { Write-Host '[MinerU] 模型检查通过，未启动服务。'; return }

function Start-LocalMinerU([string]$Name, [int]$Port, [string[]]$Arguments, [string]$ExpectedPath) {
    $url = "http://127.0.0.1:$Port/openapi.json"
    try {
        $schema = Invoke-RestMethod $url -TimeoutSec 3
        if ($schema.paths.PSObject.Properties.Name -notcontains $ExpectedPath) { throw '接口不匹配' }
        Write-Host "[MinerU] 复用已就绪的 $Name 服务：127.0.0.1:$Port"
        return
    } catch {
        $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        try { $probe.Start() } catch { throw "$Name 端口 $Port 被占用或保留，不会停止其他服务。" }
        finally { $probe.Stop() }
    }
    $process = Start-Process -FilePath $kit -ArgumentList $Arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Root "logs\$Name.stdout.log") -RedirectStandardError (Join-Path $Root "logs\$Name.stderr.log")
    Write-Host "[MinerU] 启动 $Name，PID=$($process.Id)，正在等待接口就绪..."
    for ($i = 0; $i -lt 120; $i++) {
        $process.Refresh()
        if ($process.HasExited) { throw "$Name 已退出，请查看 $Root\logs\$Name.stderr.log" }
        try {
            $schema = Invoke-RestMethod $url -TimeoutSec 2
            if ($schema.paths.PSObject.Properties.Name -contains $ExpectedPath) { return }
        } catch { }
        if ($i % 10 -eq 0) { Write-Host "[MinerU] $Name 启动中（$i/120）..." }
        Start-Sleep -Seconds 1
    }
    throw "$Name 尚未就绪，请检查D盘服务日志，不会回退解析器。"
}
Write-Host '[MinerU 2/3] 启动仅回环访问的本机解析API...'
Start-LocalMinerU 'api' 8000 @('api-server','--host','127.0.0.1','--port','8000','--tier','basic') '/v1/tasks'
Write-Host '[MinerU 3/3] 启动文件解析接口...'
Start-LocalMinerU 'file-parse' 8003 @('file-parse-server','--api-url','http://127.0.0.1:8000','--host','127.0.0.1','--port','8003','--state-dir',"`"$(Join-Path $Root 'state')`"") '/v1/file_parse'
Write-Host '[MinerU] 文件接口已就绪；尚不代表真实识别验收通过。可在工作台选择MinerU。'
