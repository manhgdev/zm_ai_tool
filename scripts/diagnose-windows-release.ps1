param(
    [Parameter(Mandatory=$true)][string]$ReleaseTag,
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Output = (Resolve-Path -LiteralPath $Output).Path
$started = Get-Date
$report = [ordered]@{
    status = 'running'; stage = 'metadata'; release = $ReleaseTag; repository = $Repository
    startedAt = $started.ToUniversalTime().ToString('o'); files = @(); scans = @(); checks = @()
}
$failure = $null
$scanner = $null
$installDir = Join-Path $env:RUNNER_TEMP 'ZM AI TOOL diagnostic'
$downloadDir = Join-Path $env:RUNNER_TEMP 'zm-ai-release-download'

function Save-Json($Value, [string]$Name) {
    ConvertTo-Json -InputObject $Value -Depth 12 | Set-Content -LiteralPath (Join-Path $Output $Name) -Encoding utf8
}

function Record-File([string]$File) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "File missing or quarantined: $File" }
    $signature = Get-AuthenticodeSignature -LiteralPath $File
    $item = [ordered]@{
        path = $File; size = (Get-Item -LiteralPath $File).Length
        sha256 = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
        signature = [string]$signature.Status; publisher = [string]$signature.SignerCertificate.Subject
    }
    $report.files += $item
    return $item
}

function Scan-File([string]$File) {
    $before = Record-File $File
    $scan = [ordered]@{ target = $File; sha256 = $before.sha256; exitCode = $null; existsAfter = $false }
    $report.scans += $scan
    $result = & $scanner -Scan -ScanType 3 -File $File 2>&1
    $scan.exitCode = $LASTEXITCODE
    $result | Out-File -LiteralPath (Join-Path $Output 'defender-scan.log') -Append -Encoding utf8
    $scan.existsAfter = Test-Path -LiteralPath $File
    if ($scan.exitCode -ne 0) { throw "Defender scan returned $($scan.exitCode): $File" }
    # Exit 0 can mean successful remediation; missing/changed bytes are a failure too.
    if (-not $scan.existsAfter -or (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant() -ne $before.sha256) {
        throw "File changed or quarantined during scan: $File"
    }
    $newThreats = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started })
    if ($newThreats.Count) { throw 'Defender recorded a detection; execution is blocked' }
}

function Run-Checked([string]$File, [string[]]$Arguments, [int]$TimeoutSeconds) {
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force
        throw "Process timed out: $File"
    }
    $process.Refresh()
    $report.checks += @{ file = $File; arguments = $Arguments; exitCode = $process.ExitCode }
    if ($process.ExitCode -ne 0) { throw "Process failed ($($process.ExitCode)): $File" }
}

try {
    if ($ReleaseTag -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Expected a release tag such as v8.6.77' }
    if ($Repository -notmatch '^[\w.-]+/[\w.-]+$') { throw 'Invalid GitHub repository' }
    $assetName = "ZM_AI_TOOL_$ReleaseTag-windows-x64-Setup.exe"
    $metadata = gh release view $ReleaseTag --repo $Repository --json tagName,assets | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read release metadata' }
    $asset = @($metadata.assets | Where-Object { $_.name -eq $assetName })
    if ($asset.Count -ne 1 -or $asset[0].digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Expected one Setup asset with a GitHub SHA-256 digest' }
    Save-Json $metadata 'release-metadata.json'
    $report.expectedSha256 = $asset[0].digest.Substring(7)
    $report.assetId = $asset[0].id
    $report.stage = 'defender-status'
    try {
        $status = Get-MpComputerStatus
        Save-Json $status 'defender-status.json'
        if (-not $status.AntivirusEnabled -or -not $status.AMServiceEnabled -or -not $status.RealTimeProtectionEnabled) {
            throw 'Defender antivirus, service, or real-time protection is disabled'
        }
        $platform = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
        $scanner = Get-ChildItem -LiteralPath $platform -Filter MpCmdRun.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
        if (-not $scanner) { $scanner = Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe' }
        if (-not (Test-Path -LiteralPath $scanner)) { throw 'Defender scanner is missing' }
    } catch {
        $report.status = 'scanner_unavailable'
        throw
    }
    $report.stage = 'signature-update'
    Update-MpSignature -ErrorAction Stop
    $status = Get-MpComputerStatus
    Save-Json $status 'defender-status.json'
    $report.signatureVersion = $status.AntivirusSignatureVersion
    $report.engineVersion = $status.AMEngineVersion
    $report.stage = 'download'
    New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
    gh release download $ReleaseTag --repo $Repository --pattern $assetName --dir $downloadDir --clobber
    if ($LASTEXITCODE -ne 0) { throw 'Could not download the original Setup asset' }
    # The application under investigation does not need repository credentials.
    Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
    $installer = Join-Path $downloadDir $assetName
    $report.stage = 'hash-verification'
    $file = Record-File $installer
    if ($file.sha256 -ne $report.expectedSha256) {
        $report.status = 'hash_mismatch'
        throw 'Downloaded Setup SHA-256 differs from the GitHub asset digest'
    }
    $report.stage = 'scan-installer'
    Scan-File $installer
    $report.stage = 'install'
    Run-Checked $installer @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=`"$installDir`"", "/LOG=`"$(Join-Path $Output 'setup.log')`"") 300
    $exe = Join-Path $installDir 'ZM AI TOOL.exe'
    $report.stage = 'inventory'
    python scripts/windows_release_audit.py --bundle $installDir --installer $installer --output $Output
    if ($LASTEXITCODE -ne 0) { throw 'Installed payload inventory failed' }
    $payload = Get-Content -LiteralPath (Join-Path $Output 'windows-artifact-manifest.json') -Raw | ConvertFrom-Json
    $report.stage = 'scan-payload'
    # Capture every binary's hash and publisher before scanning can quarantine it.
    foreach ($binary in Get-ChildItem -LiteralPath $installDir -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') }) {
        $null = Record-File $binary.FullName
    }
    $scan = [ordered]@{ target = $installDir; exitCode = $null }
    $report.scans += $scan
    $result = & $scanner -Scan -ScanType 3 -File $installDir 2>&1
    $scan.exitCode = $LASTEXITCODE
    $result | Out-File -LiteralPath (Join-Path $Output 'defender-scan.log') -Append -Encoding utf8
    if ($scan.exitCode -ne 0) { throw "Payload scan returned $($scan.exitCode)" }
    foreach ($item in $payload.files) {
        $path = Join-Path $installDir $item.path
        if (-not (Test-Path -LiteralPath $path) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
            throw "Payload changed or quarantined: $path"
        }
    }
    if (@(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started }).Count) { throw 'Defender recorded a detection; application checks are blocked' }
    $report.stage = 'executable-checks'
    foreach ($check in @('runtime-import', 'yt-dlp-version')) {
        $checkReport = Join-Path $Output "$check.json"
        Run-Checked $exe @("--$check-check", "`"$checkReport`"") 60
        if (-not (Test-Path -LiteralPath $checkReport)) { throw "Missing executable check report: $check" }
        if ($check -eq 'runtime-import' -and -not (Get-Content $checkReport -Raw | ConvertFrom-Json).ok) { throw 'Runtime import check failed' }
    }
    $report.status = 'passed'
    $report.stage = 'complete'
} catch {
    $failure = $_
    if ($report.status -eq 'running') { $report.status = 'failed' }
    $report.error = [string]$_
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $Output 'error.log')
} finally {
    Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
    try {
        $threatNames = @{}
        foreach ($threat in @(Get-MpThreat)) { $threatNames[[string]$threat.ThreatID] = $threat.ThreatName }
        $detections = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started } | ForEach-Object {
            [ordered]@{ detectionId = $_.DetectionID; threatId = $_.ThreatID; name = $threatNames[[string]$_.ThreatID]
                resources = $_.Resources; detectedAt = $_.InitialDetectionTime; actionSuccess = $_.ActionSuccess }
        })
        Save-Json $detections 'defender-detections.json'
        if ($detections.Count -gt 0) { $report.status = 'detected' }
    } catch { Save-Json @{ error = [string]$_ } 'defender-detections.json' }
    try {
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; StartTime = $started; Id = @(1116, 1117, 1118, 1119) } -ErrorAction SilentlyContinue |
            Select-Object TimeCreated, Id, Message)
        Save-Json $events 'defender-events.json'
    } catch { Save-Json @{ error = [string]$_ } 'defender-events.json' }
    $missing = @($report.files | Where-Object { -not (Test-Path -LiteralPath $_.path) } | ForEach-Object { $_.path })
    $report.missingFiles = $missing
    $report.finishedAt = (Get-Date).ToUniversalTime().ToString('o')
    Save-Json $report 'diagnosis.json'
    if ($env:GITHUB_STEP_SUMMARY) {
        "### Windows release diagnosis: $ReleaseTag`n`nStatus: **$($report.status)**; stage: $($report.stage).`n`n$($report.error)`n`nResults cover the recorded file hashes and Defender version only." |
            Out-File -LiteralPath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8
    }
}
if ($failure -or $report.status -ne 'passed') { throw "Windows diagnosis: $($report.status) at $($report.stage): $($report.error)" }
