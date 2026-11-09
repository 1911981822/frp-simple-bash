# Windows install, shared by the two single-script entry points.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('frps','frpc')][string]$Component,
    [ValidateSet('v0.72.0')][string]$Version = 'v0.72.0',
    [string]$WinSWSha256 = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer as Administrator.'
}
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'Only Windows AMD64 is supported.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$root = Join-Path $env:ProgramFiles 'frp-simple-bash'
$data = if ($env:FRP_CONFIG_DIR) { $env:FRP_CONFIG_DIR } else { Join-Path $env:ProgramData 'frp-simple-bash' }
$native = Join-Path $root 'native'
$bin = Join-Path $root 'bin'
$services = Join-Path $root 'services'
$repo = 'https://raw.githubusercontent.com/1911981822/frp-simple-bash/main'
$python = $null
foreach ($candidate in @('python','python3','py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        & $found.Source -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $found.Source; break }
    }
}
if (-not $python) { throw 'Python 3.11+ is required. Install Python and add it to PATH.' }
New-Item -ItemType Directory -Force -Path $root,$native,$bin,$services,$data,(Join-Path $data 'logs') | Out-Null
# Remove inherited and explicit access rules; grant only SYSTEM and Administrators.
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($sidText in @('S-1-5-18','S-1-5-32-544')) {
    $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $data -AclObject $acl
# Descendants inherit this restricted ACL, including any existing configuration.
& icacls.exe (Join-Path $data '*') /reset /T /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect configuration ACL.' }
function Save-RepositoryFile([string]$relative) {
    $dest = Join-Path $root $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    $local = Join-Path $PSScriptRoot $relative
    if ((Test-Path $local) -and ([IO.Path]::GetFullPath($local) -ne [IO.Path]::GetFullPath($dest))) {
        Copy-Item -LiteralPath $local -Destination $dest -Force
    } elseif (-not (Test-Path $local)) {
        Invoke-WebRequest -UseBasicParsing -Uri "$repo/$($relative.Replace('\','/'))" -OutFile $dest
    }
}
foreach ($relative in @('install-frps.ps1','install-frpc.ps1','install-windows.ps1','lib/manager.py','bin/frps.ps1','bin/frpc.ps1','bin/frps.cmd','bin/frpc.cmd','uninstall-frps.ps1','uninstall-frpc.ps1','uninstall-windows.ps1')) { Save-RepositoryFile $relative }
$temp = Join-Path $env:TEMP ('frp-simple-bash-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
$zip = Join-Path $temp 'frp.zip'
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/fatedier/frp/releases/download/v0.72.0/frp_0.72.0_windows_amd64.zip' -OutFile $zip
if ((Get-FileHash -Algorithm SHA256 $zip).Hash.ToLowerInvariant() -ne '5197c1b69564089cdc229fc1260a0e25db1e722b1b7db45fe1542c3c43edf9ae') { throw 'frp SHA256 mismatch.' }
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $temp 'frp')
$frpExe = Join-Path $temp "frp/frp_0.72.0_windows_amd64/$Component.exe"
if (-not (Test-Path $frpExe)) { throw 'Missing native binary in archive.' }
# Fixed official release asset 93386820, fetched over HTTPS on 2026-11-09.
# SHA256 is pinned from that download; it is not an upstream signed checksum.
$wrapperVersion = 'v2.12.0'
$wrapperPin = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/winsw/winsw/releases/tags/$wrapperVersion" -Headers @{Accept='application/vnd.github+json'}
$asset = @($release.assets | Where-Object { $_.name -eq 'WinSW-x64.exe' })
if ($asset.Count -ne 1 -or $asset[0].id -ne 93386820) { throw 'Pinned WinSW asset is missing, changed or ambiguous.' }
$expected = $WinSWSha256.ToLowerInvariant()
if (-not $expected) {
    $digestProperty = $asset[0].PSObject.Properties['digest']
    if ($digestProperty -and ([string]$digestProperty.Value -match '^sha256:([a-fA-F0-9]{64})$')) { $expected = $Matches[1].ToLowerInvariant() }
}
if (-not $expected) { $expected = $wrapperPin }
if ($expected -notmatch '^[a-f0-9]{64}$') { throw 'No valid WinSW SHA256 is available. Installation stopped without registering a service.' }
$wrapper = Join-Path $temp 'winsw.exe'
Invoke-WebRequest -UseBasicParsing -Uri $asset[0].browser_download_url -OutFile $wrapper
if ((Get-FileHash -Algorithm SHA256 $wrapper).Hash.ToLowerInvariant() -ne $expected) { throw 'WinSW SHA256 mismatch.' }
$serviceId = "frp-simple-bash-$Component"
$serviceExe = Join-Path $services "$serviceId.exe"
$existingService = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
$wasRunning = $existingService -and $existingService.Status -eq 'Running'
$nativeExe = Join-Path $native "$Component.exe"
$serviceXml = Join-Path $services "$serviceId.xml"
$rollback = Join-Path $temp 'rollback'
New-Item -ItemType Directory -Path $rollback | Out-Null
$restoreFiles = @($nativeExe,$serviceExe,$serviceXml)
foreach ($file in $restoreFiles) {
    if (Test-Path -LiteralPath $file) { Copy-Item -LiteralPath $file -Destination (Join-Path $rollback ([IO.Path]::GetFileName($file))) }
}
if ($existingService -and $existingService.Status -ne 'Stopped') {
    Stop-Service -Name $serviceId
    $existingService.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))
}
try {
Copy-Item -LiteralPath $frpExe -Destination (Join-Path $native "$Component.exe") -Force
Copy-Item -LiteralPath $wrapper -Destination $serviceExe -Force
$config = Join-Path $data "$Component.toml"
$escapedExe = [Security.SecurityElement]::Escape((Join-Path $native "$Component.exe"))
$escapedConfig = [Security.SecurityElement]::Escape($config)
$escapedData = [Security.SecurityElement]::Escape($data)
$xml = @"
<service>
  <id>$serviceId</id>
  <name>frp-simple-bash $Component</name>
  <description>frp v0.72.0 managed with WinSW $wrapperVersion</description>
  <executable>$escapedExe</executable>
  <arguments>-c &quot;$escapedConfig&quot;</arguments>
  <workingdirectory>$escapedData</workingdirectory>
  <startmode>Automatic</startmode>
  <stoptimeout>20 sec</stoptimeout>
  <onfailure action="restart" delay="5 sec" />
  <logpath>$escapedData\logs</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>10</keepFiles></log>
</service>
"@
[IO.File]::WriteAllText((Join-Path $services "$serviceId.xml"), $xml, $utf8)
if (-not $existingService) {
    & $serviceExe install
    if ($LASTEXITCODE -ne 0) { throw 'WinSW service registration failed.' }
}
# Preserve configuration and only invoke the wizard on first install.
Set-Service -Name $serviceId -StartupType Automatic
$machinePath = [Environment]::GetEnvironmentVariable('Path','Machine')
if (@($machinePath -split ';') -notcontains $bin) { [Environment]::SetEnvironmentVariable('Path', "$machinePath;$bin", 'Machine') }
$env:Path = "$bin;$env:Path"
$env:FRP_CONFIG_DIR = $data
$env:FRP_BINARY_DIR = $native
$env:PYTHONUTF8 = '1'
if (-not (Test-Path $config)) {
    & $python (Join-Path $root 'lib/manager.py') $Component configure
    if ($LASTEXITCODE -ne 0) { throw 'Manager initialization did not complete.' }
}
if (Test-Path $config) {
    & (Join-Path $native "$Component.exe") verify -c $config
    if ($LASTEXITCODE -ne 0) { throw 'Native configuration verification failed; service was not started.' }
    Start-Service -Name $serviceId
    (Get-Service -Name $serviceId).WaitForStatus('Running',[TimeSpan]::FromSeconds(30))
}
} catch {
    $installError = $_
    try {
        $current = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
        if ($current -and $current.Status -ne 'Stopped') {
            Stop-Service -Name $serviceId
            $current.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))
        }
        foreach ($file in $restoreFiles) {
            $backupFile = Join-Path $rollback ([IO.Path]::GetFileName($file))
            if (Test-Path -LiteralPath $backupFile) { Copy-Item -LiteralPath $backupFile -Destination $file -Force }
        }
        if ($wasRunning) { Start-Service -Name $serviceId }
    } catch { Write-Warning "Rollback could not fully restore service: $_" }
    throw $installError
}
Write-Host "Installed $Component. Open a new terminal and run $Component. Configuration: $data"
Write-Host "Temporary download directory: $temp"
