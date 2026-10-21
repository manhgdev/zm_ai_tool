param([Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $Output | Out-Null
$fields = @('ExclusionPath', 'ExclusionExtension', 'ExclusionProcess', 'DisableArchiveScanning', 'DisableRealtimeMonitoring')
try {
    if ($env:RUNNER_ENVIRONMENT -ne 'github-hosted') { throw 'Defender preparation is restricted to a disposable GitHub-hosted runner' }
    Get-MpPreference | Select-Object $fields | ConvertTo-Json -Depth 4 | Set-Content "$Output/defender-preferences-before.json"
    # GitHub's image excludes both drives. Remove exclusions so scans cover the bytes.
    Remove-MpPreference -ExclusionPath 'C:\', 'D:\' -ErrorAction Stop
    Set-MpPreference -DisableArchiveScanning $false -ErrorAction Stop
    # Request real-time protection too; audits record the effective engine state.
    Set-MpPreference -DisableRealtimeMonitoring $false -ErrorAction Stop
} catch {
    $_ | Out-String | Set-Content "$Output/defender-preparation-error.log"
    throw
} finally {
    Get-MpPreference -ErrorAction Continue | Select-Object $fields | ConvertTo-Json -Depth 4 | Set-Content "$Output/defender-preferences-after.json"
}
