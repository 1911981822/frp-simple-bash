[CmdletBinding()]
param([switch]$Purge,[switch]$Yes)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'uninstall-windows.ps1') -Component 'frpc' -Purge:$Purge -Yes:$Yes
