[CmdletBinding()]
param([switch]$Purge,[switch]$Yes)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'uninstall-windows.ps1') -Component 'frps' -Purge:$Purge -Yes:$Yes
