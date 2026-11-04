# Grow Up host-pinned release deployment. Run from Windows PowerShell.
[CmdletBinding()]
param([ValidateSet('DEV_SERVER','LIVE_SERVER')][string]$Target = 'DEV_SERVER', [switch]$PlanOnly, [switch]$AllowDirtyWorktree)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RemoteHostName = if ($Target -eq 'DEV_SERVER') { 'securecode-dev' } else { 'myvps' }
$ExpectedHost = if ($Target -eq 'DEV_SERVER') { 'securecode-dev' } else { 'srv1577410' }
$Remote = "ubuntu@${RemoteHostName}"
$ActualHost = (& ssh -o BatchMode=yes -o ConnectTimeout=12 $Remote hostname | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualHost -ne $ExpectedHost) { throw 'SSH host identity does not match deployment target.' }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dirty = @(& git -C $Repo status --porcelain)
if ($LASTEXITCODE -ne 0) { throw 'git status failed.' }
if ($dirty.Count -gt 0 -and -not $AllowDirtyWorktree -and -not $PlanOnly) { throw 'Working tree has changes. Review them, then use -AllowDirtyWorktree to deploy only the exact committed GitHub revision.' }
if ($dirty.Count -gt 0) { Write-Host 'Uncommitted files remain untouched and are excluded from the committed deployment archive.' }
$Branch = (& git -C $Repo branch --show-current | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $Branch -ne 'main') { throw 'Deployment requires local main branch.' }
$Sha = (& git -C $Repo rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $Sha -notmatch '^[0-9a-f]{40}$') { throw 'Could not determine commit.' }
$RemoteRef = (& git -C $Repo ls-remote origin refs/heads/main | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $RemoteRef -or ($RemoteRef -split '\s+')[0] -ne $Sha) {
    throw 'GitHub origin/main must contain the exact local commit.'
}
# Fail before upload/release creation if the VM root filesystem cannot
# safely accommodate another isolated virtual environment and its cache.
$DiskReport = @(& ssh -o BatchMode=yes $Remote 'df -Pk /')
if ($LASTEXITCODE -ne 0 -or $DiskReport.Count -lt 2) { throw 'Could not check target server free space.' }
$DiskFields = ($DiskReport[-1].Trim() -split '\s+')
$AvailableKiB = 0L
if ($DiskFields.Count -lt 6 -or -not [long]::TryParse($DiskFields[3], [ref]$AvailableKiB)) {
    throw 'Unexpected target server disk report; no deployment started.'
}
if ($AvailableKiB -lt 1048576) {
    throw "STOP: target server has $([math]::Round($AvailableKiB / 1024)) MiB free; free at least 1 GiB before deployment. No release was created."
}
if ($PlanOnly) {
    Write-Host "PLAN: $Target ($ActualHost), exact GitHub commit $Sha; $([math]::Round($AvailableKiB / 1024)) MiB free. No upload or deployment."
    return
}
if ($Target -eq 'LIVE_SERVER') {
    $ToolNames = @('growup-install-release', 'remote-install-release.sh', 'configure-server.py')
    $RemoteHashes = @(& ssh -o BatchMode=yes $Remote 'sha256sum /usr/local/sbin/growup-install-release /usr/local/libexec/growup/remote-install-release.sh /usr/local/libexec/growup/configure-server.py')
    if ($LASTEXITCODE -ne 0 -or $RemoteHashes.Count -ne 3) { throw 'Trusted live deployment tools are missing.' }
    for ($Index = 0; $Index -lt 3; $Index++) {
        $ExpectedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot $ToolNames[$Index])).Hash.ToLowerInvariant()
        if (($RemoteHashes[$Index] -split '\s+')[0] -ne $ExpectedHash) { throw "Installed live tool drift: $($ToolNames[$Index]). Install the reviewed tools before deploying." }
    }
}
$Release = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '_' + $Sha.Substring(0,7)
$Temp = Join-Path ([IO.Path]::GetTempPath()) ("growup-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Temp -Force | Out-Null
try {
    $Archive = Join-Path $Temp "$Release.tar"
    $Checksum = "$Archive.sha256"
    & git -C $Repo archive --format=tar "--output=$Archive" HEAD
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed.' }
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($Checksum, "$Hash  $Release.tar`n", [Text.UTF8Encoding]::new($false))
    & ssh -o BatchMode=yes $Remote 'test -w /var/www/growup/incoming'
    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu upload directory is not writable.' }
    & scp -o BatchMode=yes $Archive $Checksum "${Remote}:/var/www/growup/incoming/"
    if ($LASTEXITCODE -ne 0) { throw 'Upload failed.' }
    # The only privileged remote operation is a narrowly allowed, root-owned controller.
    $InstallArgs = if ($Target -eq 'LIVE_SERVER') { "'$Release' '$Sha'" } else { "'$Release'" }
    & ssh -o BatchMode=yes $Remote "sudo -n /usr/local/sbin/growup-install-release $InstallArgs"
    if ($LASTEXITCODE -ne 0) { throw 'Remote release installation failed.' }
    Write-Host "SUCCESS: Deployed $Sha to $Remote ($Release)"
}
finally {
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
