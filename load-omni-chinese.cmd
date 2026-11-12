@echo off
setlocal
pushd "%~dp0backend" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo Python environment missing. See docs\runbook.md and launch start.cmd first.
    popd
    pause
    exit /b 1
)
set "PYTHONPATH=%CD%"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" -u -m scripts.load_omni_chinese %*
set "LOAD_EXIT=%ERRORLEVEL%"
popd
if not "%LOAD_EXIT%"=="0" pause
exit /b %LOAD_EXIT%
