# Docker 专属用户目录放在 D 盘；不修改系统或当前终端的全局配置。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$desktop = 'D:\Docker\Desktop\Docker Desktop.exe'
$docker = 'D:\Docker\Desktop\resources\bin\docker.exe'
$pipe = 'npipe:////./pipe/dockerDesktopLinuxEngine'

try {
    if (-not (Test-Path -LiteralPath $desktop -PathType Leaf) -or
        -not (Test-Path -LiteralPath $docker -PathType Leaf)) {
        throw '未找到 D 盘 Docker Desktop，请先安装；不使用 C 盘兜底。'
    }
    Write-Host '[1/3] 检查 Docker Desktop 进程...'
    if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        $saved = @{}
        foreach ($name in @('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        try {
            $env:APPDATA = 'D:\Docker\UserData\Roaming'
            $env:LOCALAPPDATA = 'D:\Docker\UserData\Local'
            $env:TEMP = 'D:\Docker\Temp'
            $env:TMP = $env:TEMP
            New-Item -ItemType Directory -Path $env:APPDATA, $env:LOCALAPPDATA, $env:TEMP -Force | Out-Null
            Start-Process -FilePath $desktop -WindowStyle Hidden
        }
        finally {
            foreach ($name in $saved.Keys) {
                [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
            }
        }
    }
    Write-Host '[2/3] 等待本机 Linux 引擎，最多检查 24 次...'
    $ready = $false
    for ($i = 0; $i -lt 24; $i++) {
        $osType = & $docker --host $pipe info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -eq 0 -and $osType -eq 'linux') { $ready = $true; break }
        Write-Host "引擎尚未就绪（$($i + 1)/24）..."
        Start-Sleep -Seconds 5
    }
    if (-not $ready) { throw '引擎未就绪；请查看 Docker 界面，不自动重置或重启系统。' }
    $disk = 'D:\Docker\wsl\disk\docker_data.vhdx'
    if (-not (Test-Path -LiteralPath $disk -PathType Leaf)) {
        throw '未在约定 D 盘位置找到数据盘；停止，不拉取镜像。'
    }
    Write-Host '[3/3] Linux 引擎已就绪；约定 D 盘数据盘存在。'
    Write-Host "数据盘：$disk"
    Write-Host '未下载镜像、未调用模型。后续可运行 .\start-deepdoc.ps1。'
}
catch {
    Write-Host "Docker 启动未完成：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
