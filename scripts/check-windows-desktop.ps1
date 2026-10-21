param(
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Output,
    [switch]$CheckSystemTts,
    [switch]$CheckVieNeu
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
New-Item -ItemType Directory -Force $Output | Out-Null
$Output = (Resolve-Path -LiteralPath $Output).Path
$ready = Join-Path $Output ('desktop-ready-' + [guid]::NewGuid() + '.txt')
$oldReady = $env:ZM_AI_TOOL_UPDATE_READY_FILE
$started = Get-Date
$process = $null
$report = [ordered]@{ status = 'running'; coverage = 'API and native WebView startup; no AI workload exercised' }
try {
    $report.sha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
    $report.realTimeProtectionEnabled = [bool](Get-MpComputerStatus).RealTimeProtectionEnabled
    if ($CheckSystemTts) {
        $ttsReport = Join-Path $Output 'system-tts.json'
        Remove-Item -LiteralPath $ttsReport -Force -ErrorAction SilentlyContinue
        $process = Start-Process -FilePath $Exe -ArgumentList @('--system-tts-check', "`"$ttsReport`"") -PassThru
        if (-not $process.WaitForExit(150000)) { throw 'Packaged system TTS timed out after 150s' }
        $process.Refresh()
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $ttsReport)) { throw "Packaged system TTS failed: $($process.ExitCode)" }
        $report.systemTts = Get-Content -LiteralPath $ttsReport -Raw | ConvertFrom-Json
        if (-not $report.systemTts.ok) { throw 'Packaged system TTS did not produce valid audio' }
        $report.coverage = 'System TTS synthesis, API and native WebView startup; no AI model workload exercised'
    }
    if ($CheckVieNeu) {
        $ttsReport = Join-Path $Output 'vieneu-tts.json'
        Remove-Item -LiteralPath $ttsReport -Force -ErrorAction SilentlyContinue
        $process = Start-Process -FilePath $Exe -ArgumentList @('--vieneu-tts-check', "`"$ttsReport`"") -PassThru
        if (-not $process.WaitForExit(1200000)) { throw 'Packaged VieNeu installation/synthesis timed out after 20 minutes' }
        $process.Refresh()
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $ttsReport)) { throw "Packaged VieNeu TTS failed: $($process.ExitCode)" }
        $report.vieneu = Get-Content -LiteralPath $ttsReport -Raw | ConvertFrom-Json
        if (-not $report.vieneu.ok) { throw 'Packaged VieNeu did not produce valid audio' }
        $report.coverage = 'System TTS, VieNeu v3turbo ONNX/CPU, API and native WebView startup; no NVIDIA GPU exercised'
    }
    # Reuse the launcher's updater handshake, which runs after native GUI startup.
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $ready
    $process = Start-Process -FilePath $Exe -PassThru
    $report.pid = $process.Id
    $deadline = (Get-Date).AddSeconds(120)
    while (-not (Test-Path -LiteralPath $ready)) {
        if ($process.HasExited) { throw "Desktop exited before WebView startup: $($process.ExitCode)" }
        if ((Get-Date) -ge $deadline) { throw 'Desktop startup timed out after 120s' }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }
    $report.version = (Get-Content -LiteralPath $ready -Raw).Trim()
    if (-not $report.version) { throw 'Desktop readiness report is empty' }
    Start-Sleep -Seconds 10
    $process.Refresh()
    if ($process.HasExited) { throw "Desktop exited immediately after startup: $($process.ExitCode)" }
    if ((Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $report.sha256) { throw 'Main EXE changed during desktop startup' }
    $report.status = 'passed'
} catch {
    $report.status = 'failed'
    $report.error = [string]$_
} finally {
    $env:ZM_AI_TOOL_UPDATE_READY_FILE = $oldReady
    if ($process -and -not $process.HasExited) {
        # The supervisor owns the UI child; stop only this test's process tree.
        try {
            & "$env:SystemRoot/System32/taskkill.exe" /PID $process.Id /T /F 2>&1 |
                Out-File -LiteralPath (Join-Path $Output 'desktop-cleanup.log') -Encoding utf8
        } catch { $report.cleanupError = [string]$_ }
    }
    try {
        $detections = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $started })
        ConvertTo-Json -InputObject $detections -Depth 8 | Set-Content (Join-Path $Output 'desktop-detections.json') -Encoding utf8
        if ($detections.Count) { $report.status = 'failed'; $report.error = 'Defender recorded a threat during desktop startup' }
    } catch { $report.status = 'failed'; $report.detectionReadError = [string]$_ }
    $exeDir = Split-Path $Exe -Parent
    foreach ($log in @((Join-Path $exeDir 'app.log'), (Join-Path $exeDir 'user-data/app.log'))) {
        if (Test-Path -LiteralPath $log) {
            Get-Content -LiteralPath $log -Tail 100 | Set-Content (Join-Path $Output 'desktop-app-tail.log') -Encoding utf8
        }
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Output 'desktop-startup.json') -Encoding utf8
}
if ($report.status -ne 'passed') { throw "Desktop startup failed: $($report.error)" }
