[CmdletBinding()]
param([ValidateSet('v0.72.0')][string]$Version='v0.72.0', [string]$WinSWSha256='')
$ErrorActionPreference='Stop'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$ManagerVersion='v0.1.25-dashboard.1'
# Follow redirects explicitly so HTTPS can never downgrade to HTTP.
function Get-HttpsFile([string]$Url, [string]$Destination) {
    $uri = [Uri]$Url
    for ($hop = 0; $hop -lt 10; $hop++) {
        if ($uri.Scheme -ne 'https') { throw 'Refusing non-HTTPS source or redirect.' }
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.AllowAutoRedirect = $false
        $request.Timeout = 120000
        $request.ReadWriteTimeout = 120000
        $request.UserAgent = 'frp-simple-bash/v0.1.25-dashboard.1'
        $response = $request.GetResponse()
        try {
            $status = [int]$response.StatusCode
            if ($status -in @(301,302,303,307,308)) {
                $uri = [Uri]::new($uri, [string]$response.Headers['Location'])
                continue
            }
            if ($status -ne 200) { throw ('Unexpected HTTP response: ' + $status) }
            $output = [IO.File]::Create($Destination)
            try { $response.GetResponseStream().CopyTo($output) } finally { $output.Dispose() }
            return
        } finally { $response.Close() }
    }
    throw 'Too many HTTPS redirects.'
}

$mirrorValue=[Environment]::GetEnvironmentVariable('FRP_MIRROR_URL')
$hasMirrorVariable=Test-Path Env:FRP_MIRROR_URL
$mirror=if ($hasMirrorVariable) { [string]$mirrorValue } else { 'https://download.sueiyuan.top' }
if ($mirror -eq 'off') { $mirror='' }
if ($mirror -and $mirror -notmatch '^https://') { throw 'Mirror URL must use HTTPS, or be empty/off.' }
$installer=if ($PSScriptRoot) { Join-Path $PSScriptRoot 'install-windows.ps1' } else { '' }
$temporary=$null
try {
    if (-not $installer -or -not (Test-Path -LiteralPath $installer)) {
        $temporary=Join-Path $env:TEMP ('frp-bootstrap-'+[guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temporary | Out-Null
        $installer=Join-Path $temporary 'install-windows.ps1'
        $urls=@()
        if ($mirror) { $urls+=($mirror.TrimEnd('/')+'/frp-simple-bash/install-windows.ps1') }
        $urls+='https://raw.githubusercontent.com/1911981822/frp-simple-bash/main/install-windows.ps1'
        $ok=$false
        foreach ($url in $urls) {
            try {
                Get-HttpsFile $url ($installer+'.partial')
                Move-Item -LiteralPath ($installer+'.partial') -Destination $installer -Force
                # Clear the Internet Zone marker so the host policy cannot block it.
                try { Unblock-File -LiteralPath $installer -ErrorAction SilentlyContinue } catch {}
                $ok=$true; break
            } catch { Write-Warning ('Download failed: '+$url) }
        }
        if (-not $ok) { throw 'All installer download sources failed.' }
    }
    # The downloaded installer is a .ps1 on disk, which the host execution policy
    # (Restricted/RemoteSigned) may block. Spawn a child PowerShell that bypasses
    # the policy for this file only, mirroring the .cmd launcher.
    $childArgs = @('-Component', 'frps', '-Version', $Version)
    if ($WinSWSha256) { $childArgs += @('-WinSWSha256', $WinSWSha256) }
    & powershell.exe -ExecutionPolicy Bypass -NoProfile -NoLogo -File $installer @childArgs
    if ($LASTEXITCODE -ne 0) { throw ('Installer exited with code ' + $LASTEXITCODE) }
} finally {
    if ($temporary) {
        $resolved=[IO.Path]::GetFullPath($temporary)
        if ($resolved.StartsWith([IO.Path]::GetFullPath($env:TEMP)+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    }
}
