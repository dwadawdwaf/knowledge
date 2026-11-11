param([string]$OpenSearchHome = "$PSScriptRoot/backend/.data/windows-retrieval-v1/opensearch-3.9.0")
$ErrorActionPreference = 'Stop'
$logs = Join-Path $PSScriptRoot 'backend/.data/ragflow-native'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
try {
    $info = Invoke-RestMethod 'http://127.0.0.1:19276/' -TimeoutSec 2
    if ($info.version.distribution -ne 'opensearch') { throw 'Port is not OpenSearch' }
    Write-Host 'OpenSearch is already running on port 19276.'
    exit 0
} catch { }
if (-not (Test-Path -LiteralPath (Join-Path $OpenSearchHome 'bin/opensearch.bat'))) {
    throw 'OpenSearch installation missing. Set -OpenSearchHome; no automatic download.'
}
$env:OPENSEARCH_JAVA_HOME = Join-Path $OpenSearchHome 'jdk'
$env:OPENSEARCH_JAVA_OPTS = '-Xms512m -Xmx512m'
$process = Start-Process -FilePath (Join-Path $OpenSearchHome 'bin/opensearch.bat') -WorkingDirectory $OpenSearchHome `
    -ArgumentList @('-Ediscovery.type=single-node','-Enetwork.host=127.0.0.1','-Ehttp.port=19276',
        '-Etransport.port=19376','-Eplugins.security.disabled=true','-Enode.name=ks-ragflow-native') `
    -RedirectStandardOutput (Join-Path $logs 'opensearch.stdout.log') `
    -RedirectStandardError (Join-Path $logs 'opensearch.stderr.log') -PassThru -WindowStyle Hidden
$process.Id | Set-Content (Join-Path $logs 'opensearch.pid')
for ($attempt=0; $attempt -lt 60; $attempt++) {
    try {
        $info = Invoke-RestMethod 'http://127.0.0.1:19276/' -TimeoutSec 1
        if ($info.version.distribution -eq 'opensearch') { Write-Host 'OpenSearch ready.'; exit 0 }
    } catch { }
    if ($process.HasExited) { throw 'OpenSearch exited; inspect backend/.data/ragflow-native logs.' }
    Start-Sleep -Seconds 1
}
throw 'OpenSearch startup timed out; inspect backend/.data/ragflow-native logs.'
