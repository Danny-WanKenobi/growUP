# Explicit target wrapper; propagate failure to interactive callers and automation.
[CmdletBinding()]
param([switch]$PlanOnly,[switch]$AllowDirtyWorktree)
try {
    & (Join-Path $PSScriptRoot 'deploy.ps1') -Target LIVE_SERVER -PlanOnly:$PlanOnly -AllowDirtyWorktree:$AllowDirtyWorktree
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
