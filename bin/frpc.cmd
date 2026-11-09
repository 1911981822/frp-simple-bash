@echo off
setlocal
set "PYTHONUTF8=1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0frpc.ps1" %*
exit /b %errorlevel%
