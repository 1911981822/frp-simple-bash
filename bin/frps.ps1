# ASCII bootstrap supports Windows PowerShell 5.1 without a BOM.
$ErrorActionPreference='Stop'
$utf8=New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding=$utf8
[Console]::OutputEncoding=$utf8
$OutputEncoding=$utf8
$env:PYTHONUTF8='1'
$root=Split-Path $PSScriptRoot -Parent
if (-not $env:FRP_CONFIG_DIR) { $env:FRP_CONFIG_DIR=Join-Path $env:ProgramData 'frp-simple-bash' }
if (-not $env:FRP_BINARY_DIR) { $env:FRP_BINARY_DIR=Join-Path $root 'native' }
$python=$null
foreach ($name in @('python','python3','py')) {
    $found=Get-Command $name -ErrorAction SilentlyContinue
    if ($found) {
        & $found.Source -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $python=$found.Source; break }
    }
}
if (-not $python) { throw 'Python 3.11+ is required.' }
$manager=Join-Path $root 'lib/manager.py'
if (-not (Test-Path $manager)) { throw "Manager not found: $manager" }
& $python $manager frps @args
exit $LASTEXITCODE
