[CmdletBinding()]
param([string]$OpenSearchRoot)

$ErrorActionPreference = 'Stop'
$url = 'http://127.0.0.1:19277/'
$cluster = 'ks-workbench-local'

function Test-Search {
    try {
        $raw = & curl.exe --noproxy '*' --fail --silent --max-time 2 $url
        if ($LASTEXITCODE -ne 0) { return $false }
        $status = $raw | ConvertFrom-Json
        return ($status.cluster_name -eq $cluster -and $status.version.distribution -eq 'opensearch')
    }
    catch { return $false }
}

if (Test-Search) { return }
if (-not $OpenSearchRoot) {
    $OpenSearchRoot = Join-Path $PSScriptRoot 'backend/.data/opensearch-3.9.0'
    if (-not (Test-Path -LiteralPath $OpenSearchRoot -PathType Container)) {
        $OpenSearchRoot = Join-Path $PSScriptRoot 'backend/.data/windows-retrieval-v1/opensearch-3.9.0'
    }
}
$launcher = Join-Path $OpenSearchRoot 'bin/opensearch.bat'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw 'OpenSearch 3.9.0 was not found. Extract the Windows distribution to backend/.data/opensearch-3.9.0.'
}
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 19277)
try { $listener.Start() }
catch { throw 'Port 19277 is in use by another service. Configure KS_OPENSEARCH_URL for a dedicated local instance.' }
finally { $listener.Stop() }

$runtime = Join-Path $PSScriptRoot 'backend/.data/workbench-opensearch'
$data = Join-Path $runtime 'data'
$logs = Join-Path $runtime 'logs'
New-Item -ItemType Directory -Path $data,$logs -Force | Out-Null
$javaHome = $env:OPENSEARCH_JAVA_HOME
$javaOpts = $env:OPENSEARCH_JAVA_OPTS
try {
    $env:OPENSEARCH_JAVA_HOME = Join-Path $OpenSearchRoot 'jdk'
    $env:OPENSEARCH_JAVA_OPTS = '-Xms512m -Xmx512m'
    $process = Start-Process -FilePath $launcher -WorkingDirectory $OpenSearchRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-Ediscovery.type=single-node', '-Enetwork.host=127.0.0.1', '-Ehttp.port=19277',
            '-Etransport.port=19377', '-Eplugins.security.disabled=true', "-Ecluster.name=$cluster",
            '-Enode.name=ks-workbench', ('"-Epath.data=' + $data + '"'), ('"-Epath.logs=' + $logs + '"')) `
        -RedirectStandardOutput (Join-Path $runtime 'stdout.log') -RedirectStandardError (Join-Path $runtime 'stderr.log')
}
finally {
    $env:OPENSEARCH_JAVA_HOME = $javaHome
    $env:OPENSEARCH_JAVA_OPTS = $javaOpts
}
for ($i = 0; $i -lt 55; $i++) {
    if (Test-Search) { return }
    if ($process.HasExited) { break }
    Start-Sleep -Seconds 1
}
throw 'OpenSearch did not become ready. See backend/.data/workbench-opensearch logs.'
