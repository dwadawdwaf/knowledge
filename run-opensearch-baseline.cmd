@echo off
rem 在主机上运行 OpenSearch 检索基线（复用本机 OpenSearch，不写沙箱）。
rem 用法：run-opensearch-baseline.cmd [-Resume] [-SkipOcr] [-SkipPostgresCompare]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\testdata\rag-baseline-v1\run_opensearch_host.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo.
  echo 运行失败，退出码 %EXITCODE%。请把上面的输出发给 Claude。
)
pause
exit /b %EXITCODE%
