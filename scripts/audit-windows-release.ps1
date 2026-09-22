param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Portable,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Bundle = (Resolve-Path -LiteralPath $Bundle).Path
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$Portable = (Resolve-Path -LiteralPath $Portable).Path
$manifestPath = Join-Path $Output 'windows-artifact-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Artifact manifest must be created before scanning' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$signatures = @()
$binaryPaths = @(Get-ChildItem -LiteralPath $Bundle -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') } | ForEach-Object { $_.FullName }) + @($Installer)
foreach ($file in $binaryPaths) {
    $sig = Get-AuthenticodeSignature -LiteralPath $file
    $signatures += [ordered]@{ file = $file; sha256 = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant(); status = [string]$sig.Status; publisher = [string]$sig.SignerCertificate.Subject }
    if ($sig.Status -notin @('Valid', 'NotSigned')) { throw "Invalid signature on $file : $($sig.Status)" }
}
$signatures | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Output 'windows-signatures.json') -Encoding utf8
$status = Get-MpComputerStatus
if (-not $status.AntivirusEnabled -or -not $status.AMServiceEnabled) { throw 'Defender is not available: release is blocked, not marked clean' }
Update-MpSignature
$status = Get-MpComputerStatus
$started = Get-Date
$platformDir = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
$command = Get-ChildItem -LiteralPath $platformDir -Filter MpCmdRun.exe -Recurse | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $command) { throw 'Defender scanner not found' }
$scanReport = [ordered]@{ status = 'running'; signatureVersion = $status.AntivirusSignatureVersion; engineVersion = $status.AMEngineVersion; startedAt = $started.ToUniversalTime().ToString('o'); scans = @() }
try {
    foreach ($target in @($Bundle, $Installer, $Portable)) {
        $result = & $command.FullName -Scan -ScanType 3 -File $target 2>&1
        $code = $LASTEXITCODE
        $result | Out-File -LiteralPath (Join-Path $Output 'defender-scan.log') -Append -Encoding utf8
        $scanReport.scans += @{ target = $target; exitCode = $code }
        if ($code -ne 0) { throw "Defender scan failed or detected a threat: exit $code. No release will be published." }
    }
    $detections = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started })
    $detections | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'defender-detections.json') -Encoding utf8
    if ($detections.Count -gt 0) { throw 'Defender recorded new detections during release audit' }
    # Exit 0 can include successful remediation. Hash/absence checks below
    # ensure Defender has not removed or changed any file since inventory.
    foreach ($item in $manifest.files) {
        $path = Join-Path $Bundle $item.path
        if (-not (Test-Path -LiteralPath $path) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw "Payload changed or quarantined: $path" }
    }
    foreach ($asset in $manifest.assets) {
        $path = if ($asset.name -eq (Split-Path $Installer -Leaf)) { $Installer } else { $Portable }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) { throw "Artifact changed: $path" }
    }
    $scanReport.status = 'passed'
} catch {
    $scanReport.status = 'failed'
    throw
} finally {
    $scanReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'windows-defender-report.json') -Encoding utf8
}
