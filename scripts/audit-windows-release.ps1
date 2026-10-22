param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Bundle = (Resolve-Path -LiteralPath $Bundle).Path
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$manifestPath = Join-Path $Output 'windows-artifact-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Artifact manifest must be created before scanning' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$binaryPaths = @(Get-ChildItem -LiteralPath $Bundle -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') } | ForEach-Object { $_.FullName }) + @($Installer)
$started = Get-Date
$scanReport = [ordered]@{ status = 'running'; startedAt = $started.ToUniversalTime().ToString('o'); scans = @(); exclusions = @(); coverage = 'On-demand file scans only; not a runtime protection verdict' }
try {
    $signatures = @()
    foreach ($file in $binaryPaths) {
        $sig = Get-AuthenticodeSignature -LiteralPath $file
        $signatures += [ordered]@{ file = $file; sha256 = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant(); status = [string]$sig.Status; publisher = [string]$sig.SignerCertificate.Subject }
        if ($sig.Status -notin @('Valid', 'NotSigned')) { throw "Invalid signature on $file : $($sig.Status)" }
    }
    $signatures | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Output 'windows-signatures.json') -Encoding utf8
    $status = Get-MpComputerStatus
    if (-not $status.AntivirusEnabled -or -not $status.AMServiceEnabled) { throw 'Defender is not available: release is blocked, not marked clean' }
    try {
        Update-MpSignature -ErrorAction Stop
        $status = Get-MpComputerStatus
    } catch {
        # A failed update can leave the Defender WMI provider unloadable ("Provider load failure"),
        # so keep the status read above; definitions are unchanged anyway.
        Write-Warning "Update-MpSignature failed (network/infra issue): $_  Continuing with existing definitions."
        $scanReport.signatureUpdateError = [string]$_
    }
    $scanReport.signatureVersion = $status.AntivirusSignatureVersion
    $scanReport.engineVersion = $status.AMEngineVersion
    $scanReport.realTimeProtectionEnabled = [bool]$status.RealTimeProtectionEnabled
    Get-MpPreference | Select-Object ExclusionPath, ExclusionExtension, ExclusionProcess, DisableArchiveScanning, DisableRealtimeMonitoring |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Output 'defender-preferences.json') -Encoding utf8
    $platformDir = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
    $command = Get-ChildItem -LiteralPath $platformDir -Filter MpCmdRun.exe -Recurse | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $command) { throw 'Defender scanner not found' }
    # Check actual paths, including nested binaries and the elevated driver script.
    # A successful scan can skip excluded files (GitHub's images exclude whole drives).
    $scripts = @(Get-ChildItem -LiteralPath $Bundle -Recurse -File -Filter '*.ps1' | ForEach-Object { $_.FullName })
    foreach ($target in @($Bundle) + $binaryPaths + $scripts) {
        $result = & $command.FullName -CheckExclusion -Path $target 2>&1
        $code = $LASTEXITCODE
        $scanReport.exclusions += @{ path = $target; exitCode = $code }
        $result | Out-File -LiteralPath (Join-Path $Output 'defender-exclusions.log') -Append -Encoding utf8
        if ($code -eq 0) {
            $scanReport.status = 'scan_excluded'
            throw "Defender excludes this scan target: $target"
        }
        if ($code -ne 1) { throw "Could not verify scan exclusions: $target (exit $code)" }
    }
    foreach ($target in @($Bundle, $Installer)) {
        $result = & $command.FullName -Scan -ScanType 3 -File $target 2>&1
        $code = $LASTEXITCODE
        $result | Out-File -LiteralPath (Join-Path $Output 'defender-scan.log') -Append -Encoding utf8
        $scanReport.scans += @{ target = $target; exitCode = $code }
        if ($code -ne 0) { throw "Defender scan failed or detected a threat: exit $code. No release will be published." }
    }
    $detections = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started })
    if ($detections.Count -gt 0) { throw 'Defender recorded new detections during release audit' }
    # Exit 0 can include successful remediation. Hash/absence checks below
    # ensure Defender has not removed or changed any file since inventory.
    foreach ($item in $manifest.files) {
        $path = Join-Path $Bundle $item.path
        if (-not (Test-Path -LiteralPath $path) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw "Payload changed or quarantined: $path" }
    }
    foreach ($asset in $manifest.assets) {
        if ($asset.name -ne (Split-Path $Installer -Leaf)) { throw "Unexpected release artifact: $($asset.name)" }
        $path = $Installer
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) { throw "Artifact changed: $path" }
    }
    $scanReport.status = 'passed'
} catch {
    if ($scanReport.status -eq 'running') { $scanReport.status = 'failed' }
    $scanReport.error = [string]$_
    throw
} finally {
    try {
        $detections = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started })
        ConvertTo-Json -InputObject $detections -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'defender-detections.json') -Encoding utf8
        if ($detections.Count -gt 0 -and $scanReport.status -eq 'passed') {
            $scanReport.status = 'failed'
            $scanReport.error = 'Defender recorded new detections during release audit'
        }
    } catch {
        $scanReport.detectionReadError = [string]$_
        if ($scanReport.status -eq 'passed') { $scanReport.status = 'failed' }
    }
    $scanReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'windows-defender-report.json') -Encoding utf8
}
if ($scanReport.status -ne 'passed') { throw "Windows audit: $($scanReport.status)" }
