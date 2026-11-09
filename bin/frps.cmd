@echo off
setlocal
set "PYTHONUTF8=1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0frps.ps1" %*
exit /b %errorlevel%
