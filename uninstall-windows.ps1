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
    if (-not $Yes -and (Read-Host "Purge $Component configuration and profiles in $data? Type DELETE") -cne 'DELETE') { Write-Host 'Configuration retained.'; return }
    $resolved=[IO.Path]::GetFullPath($data).TrimEnd('\')
    $default=[IO.Path]::GetFullPath((Join-Path $env:ProgramData 'frp-simple-bash')).TrimEnd('\')
    if (-not $resolved.Equals($default,[StringComparison]::OrdinalIgnoreCase)) { throw 'Automatic purge is allowed only for the default configuration directory. Custom configuration retained.' }
    $other=if ($Component -eq 'frps') { 'frpc' } else { 'frps' }

    # 1) Always delete THIS component's own config (purge = 彻底清理本组件),
    #    never blocked by the other component still being installed.
    if ($Component -eq 'frpc') {
        foreach ($rel in @('frpc.toml','profiles','index.json')) {
            $target = Join-Path $resolved $rel
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        }
    } else {
        foreach ($rel in @('frps.toml','.canonical-frps.toml')) {
            $target = Join-Path $resolved $rel
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        }
    }

    # 2) Shared dirs ($data + $root) are removed only when the other component
    #    is also gone. If it remains, keep shared dirs, drop only this one's files.
    $otherTraces=@()
    if (Get-Service -Name "frp-simple-bash-$other" -ErrorAction SilentlyContinue) { $otherTraces += "service frp-simple-bash-$other" }
    foreach ($trace in @((Join-Path $root "bin/$other.cmd"), (Join-Path $root "bin/$other.ps1"),
                         (Join-Path $root "services/frp-simple-bash-$other.exe"),
                         (Join-Path $root "native/$other.exe"))) {
        if (Test-Path -LiteralPath $trace) { $otherTraces += $trace }
    }
    if ($otherTraces.Count -gt 0) {
        Write-Host "Removed $Component configuration; $other still installed, shared data retained ($resolved)."
        Write-Host ("  $other traces: " + ($otherTraces -join ', '))
        Write-Host "  To also remove $other: run its uninstaller with -Purge."
        return
    }

    # 3) Neither component remains: remove shared data and manager root.
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
} else { Write-Host "Configuration retained: $data" }
# Shared Python manager remains available if the other component is installed.
if (-not (Test-Path (Join-Path $root 'bin/frps.cmd')) -and -not (Test-Path (Join-Path $root 'bin/frpc.cmd'))) {
    $bin=Join-Path $root 'bin'
    $path=[Environment]::GetEnvironmentVariable('Path','Machine')
    [Environment]::SetEnvironmentVariable('Path',(($path -split ';' | Where-Object { $_ -and $_ -ne $bin }) -join ';'),'Machine')
}
Write-Host "Uninstalled $Component."
