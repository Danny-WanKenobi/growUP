# Grow Up development release deployment. Run from Windows PowerShell.
[CmdletBinding()]
param([string]$RemoteHostName = 'securecode-dev', [string]$RemoteUser = 'ubuntu')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Remote = "${RemoteUser}@${RemoteHostName}"
$dirty = @(& git -C $Repo status --porcelain)
if ($LASTEXITCODE -ne 0) { throw 'git status failed.' }
if ($dirty.Count -gt 0) { throw 'Working tree is not clean. Commit or remove changes before deployment.' }
$Branch = (& git -C $Repo branch --show-current | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $Branch -ne 'main') { throw 'Deployment requires local main branch.' }
$Sha = (& git -C $Repo rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $Sha -notmatch '^[0-9a-f]{40}$') { throw 'Could not determine commit.' }
$RemoteRef = (& git -C $Repo ls-remote origin refs/heads/main | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $RemoteRef -or ($RemoteRef -split '\s+')[0] -ne $Sha) {
    throw 'GitHub origin/main must contain the exact local commit.'
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
    & ssh -o BatchMode=yes $Remote "sudo -n /usr/local/sbin/growup-install-release '$Release'"
    if ($LASTEXITCODE -ne 0) { throw 'Remote release installation failed.' }
    Write-Host "SUCCESS: Deployed $Sha to $Remote ($Release)"
}
finally {
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
