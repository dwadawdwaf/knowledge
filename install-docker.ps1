# Docker 仅装 D 盘；保留 BOM 以兼容 Windows PowerShell 5.1 中文提示。
[CmdletBinding()]
param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
$root = 'D:\Docker'
$installer = Join-Path $root 'Downloads\DockerDesktopInstaller.exe'
$desktop = Join-Path $root 'Desktop\Docker Desktop.exe'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

try {
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw '缺少 D:\Docker\Downloads\DockerDesktopInstaller.exe；请先从 Docker 官方网站下载到该位置。'
    }
    Write-Host '[1/3] 校验安装包数字签名...'
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Docker') {
        throw '安装包不是有效的 Docker 签名文件，拒绝执行。'
    }
    if (-not $isAdmin) {
        if ($Elevated) { throw '当前进程仍无管理员权限，未安装。' }
        Write-Host '[2/3] 请确认 Windows 管理员提示；程序和数据将指定到 D:\Docker。'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Elevated'
        $child = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
        while (-not $child.WaitForExit(15000)) {
            Write-Host '安装仍在进行；日志位于 D:\Docker\Downloads\installer.stdout.log。'
        }
        $child.Refresh()
        if ($child.ExitCode -ne 0) { throw "管理员安装进程退出码：$($child.ExitCode)。请检查安装日志。" }
    }
    else {
        New-Item -ItemType Directory -Path (Join-Path $root 'Temp') -Force | Out-Null
        $env:TEMP = Join-Path $root 'Temp'
        $env:TMP = $env:TEMP
        Write-Host '[2/3] 安装 Docker Desktop 到 D 盘，不启用 Windows containers...'
        $arguments = @('install', '--accept-license', '--backend=wsl-2', '--no-windows-containers',
                       '--installation-dir=D:\Docker\Desktop', '--wsl-default-data-root=D:\Docker\wsl')
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $root 'Downloads\installer.stdout.log') `
            -RedirectStandardError (Join-Path $root 'Downloads\installer.stderr.log')
        if ($process.ExitCode -ne 0) {
            throw "安装器退出码 $($process.ExitCode)；如需重启，请自行确认，不会自动重启。"
        }
    }
    if (-not (Test-Path -LiteralPath $desktop -PathType Leaf)) {
        throw '未在 D:\Docker\Desktop 找到安装后的程序；禁止使用 C 盘安装作为兜底。'
    }
    Write-Host '[3/3] 程序路径检查通过。仍需启动引擎并核实 WSL 数据盘路径后才能拉取镜像。'
}
catch {
    Write-Host "Docker 安装未完成：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
