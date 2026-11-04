#Requires -RunAsAdministrator
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskHosts = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
$taskOriginal = [System.IO.File]::ReadAllText($taskHosts)
$taskExists = $false
foreach ($taskLine in ($taskOriginal -split "`r?`n")) {
    $taskFields = ($taskLine.Split('#')[0].Trim() -split '\s+')
    if ($taskFields.Count -gt 1 -and $taskFields[0] -eq '127.0.0.1' -and $taskFields[1..($taskFields.Count-1)] -contains 'growup.local') { $taskExists = $true }
}
if (-not $taskExists) {
    $taskBackup = Join-Path $env:TEMP ('growup-hosts-' + (Get-Date -Format 'yyyyMMddTHHmmss') + '.bak')
    Copy-Item -LiteralPath $taskHosts -Destination $taskBackup
    [System.IO.File]::AppendAllText($taskHosts, "`r`n127.0.0.1 growup.local`r`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host "Added only growup.local. Backup: $taskBackup"
}
& ipconfig /flushdns
Write-Host 'Resolved addresses:'
[System.Net.Dns]::GetHostAddresses('growup.local') | ForEach-Object { Write-Host $_.IPAddressToString }
