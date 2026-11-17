@echo off
setlocal
set "HERE=%~dp0"
set "BASE=%HERE%.."
python "%BASE%\lib\webui.py" %*
