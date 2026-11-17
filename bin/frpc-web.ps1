#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
& python (Join-Path $base 'lib/webui.py') @args
exit $LASTEXITCODE
