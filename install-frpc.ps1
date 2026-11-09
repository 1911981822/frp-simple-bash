[CmdletBinding()]
param([ValidateSet('v0.72.0')][string]$Version='v0.72.0', [string]$WinSWSha256='')
$ErrorActionPreference='Stop'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$installer=if ($PSScriptRoot) { Join-Path $PSScriptRoot 'install-windows.ps1' } else { Join-Path $env:TEMP ('frp-bootstrap-'+[guid]::NewGuid().ToString('N')+'.ps1') }
if (-not (Test-Path $installer)) {
    $installer=Join-Path $env:TEMP ('frp-installer-'+[guid]::NewGuid().ToString('N')+'.ps1')
    Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/1911981822/frp-simple-bash/main/install-windows.ps1' -OutFile $installer
}
& $installer -Component 'frpc' -Version $Version -WinSWSha256 $WinSWSha256
