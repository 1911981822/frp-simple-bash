[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('frps','frpc')][string]$Component, [switch]$Purge, [switch]$Yes)
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as Administrator.' }
if (-not $Yes -and (Read-Host "Uninstall $Component? Type YES to confirm") -cne 'YES') { return }
$root=Join-Path $env:ProgramFiles 'frp-simple-bash'
$data=if ($env:FRP_CONFIG_DIR) { $env:FRP_CONFIG_DIR } else { Join-Path $env:ProgramData 'frp-simple-bash' }
$serviceId="frp-simple-bash-$Component"
$wrapper=Join-Path $root "services/$serviceId.exe"
$service=Get-Service -Name $serviceId -ErrorAction SilentlyContinue
if ($service) {
    if (-not (Test-Path $wrapper)) { throw 'WinSW wrapper missing. Service must be removed manually before uninstalling files.' }
    if ($service.Status -ne 'Stopped') {
        Stop-Service -Name $serviceId
        $service.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))
    }
    & $wrapper uninstall
    if ($LASTEXITCODE -ne 0) { throw 'Service uninstall failed; files retained.' }
}
# Deletion targets are explicit files under the fixed installation root.
foreach ($relative in @("native/$Component.exe","bin/$Component.cmd","bin/$Component.ps1","services/$serviceId.exe","services/$serviceId.xml")) {
    $target=[IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $target.StartsWith([IO.Path]::GetFullPath($root)+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid deletion target.' }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
}
if ($Purge) {
    if ((Read-Host "Purge ALL configuration and profiles in $data? Type DELETE") -cne 'DELETE') { Write-Host 'Configuration retained.'; return }
    $resolved=[IO.Path]::GetFullPath($data).TrimEnd('\')
    $default=[IO.Path]::GetFullPath((Join-Path $env:ProgramData 'frp-simple-bash')).TrimEnd('\')
    if (-not $resolved.Equals($default,[StringComparison]::OrdinalIgnoreCase)) { throw 'Automatic purge is allowed only for the default configuration directory. Custom configuration retained.' }
    $other=if ($Component -eq 'frps') { 'frpc' } else { 'frps' }
    if (Get-Service -Name "frp-simple-bash-$other" -ErrorAction SilentlyContinue) { throw 'Another frp service is installed. Shared configuration retained.' }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
} else { Write-Host "Configuration retained: $data" }
# Shared Python manager remains available if the other component is installed.
if (-not (Test-Path (Join-Path $root 'bin/frps.cmd')) -and -not (Test-Path (Join-Path $root 'bin/frpc.cmd'))) {
    $bin=Join-Path $root 'bin'
    $path=[Environment]::GetEnvironmentVariable('Path','Machine')
    [Environment]::SetEnvironmentVariable('Path',(($path -split ';' | Where-Object { $_ -and $_ -ne $bin }) -join ';'),'Machine')
}
Write-Host "Uninstalled $Component."
