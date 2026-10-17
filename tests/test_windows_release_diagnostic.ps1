# Native PowerShell checks for failure reporting, without downloading or running an EXE.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([System.IO.Path]::GetTempPath()) ('zm-diagnostic-test-' + [guid]::NewGuid())
$script = Join-Path $PSScriptRoot '../scripts/diagnose-windows-release.ps1'
$oldTemp, $oldData, $oldPrograms = $env:RUNNER_TEMP, $env:ProgramData, $env:ProgramFiles
New-Item -ItemType Directory -Force -Path $root | Out-Null
$env:RUNNER_TEMP = $root
$env:ProgramData = $root
$env:ProgramFiles = $root
$global:diagnosticTestActive = $false
function Get-MpComputerStatus { [pscustomobject]@{ AntivirusEnabled = $global:diagnosticTestActive; AMServiceEnabled = $global:diagnosticTestActive; RealTimeProtectionEnabled = $global:diagnosticTestActive; AntivirusSignatureVersion = 'test'; AMEngineVersion = 'test' } }
function Update-MpSignature { }
function Get-MpThreat { @() }
function Get-MpThreatDetection { @() }
function Get-WinEvent { @() }
function Get-AuthenticodeSignature { [pscustomobject]@{ Status = 'NotSigned'; SignerCertificate = $null } }
function gh {
    $global:LASTEXITCODE = 0
    if ($args[1] -eq 'view') {
        return '{"tagName":"v8.6.77","assets":[{"name":"ZM_AI_TOOL_v8.6.77-windows-x64-Setup.exe","id":"test","digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}]}'
    }
    $destination = $args[[array]::IndexOf($args, '--dir') + 1]
    Set-Content -LiteralPath (Join-Path $destination 'ZM_AI_TOOL_v8.6.77-windows-x64-Setup.exe') -Value 'hash mismatch test; never executable'
}
try {
    foreach ($expected in @('scanner_unavailable', 'hash_mismatch')) {
        $global:diagnosticTestActive = $expected -eq 'hash_mismatch'
        if ($global:diagnosticTestActive) {
            $defender = Join-Path $root 'Windows Defender'
            New-Item -ItemType Directory -Force -Path $defender | Out-Null
            # Scanner is checked for existence, but must never execute in these cases.
            Set-Content -LiteralPath (Join-Path $defender 'MpCmdRun.exe') -Value 'never execute'
        }
        $output = Join-Path $root $expected
        $failed = $false
        try { & $script -ReleaseTag v8.6.77 -Repository owner/repo -Output $output } catch { $failed = $true }
        if (-not $failed) { throw "Expected a failed diagnosis: $expected" }
        $report = Get-Content -LiteralPath (Join-Path $output 'diagnosis.json') -Raw | ConvertFrom-Json
        if ($report.status -ne $expected) { throw "Expected $expected, got $($report.status): $($report.error)" }
        foreach ($name in @('defender-detections.json', 'defender-events.json', 'error.log')) {
            if (-not (Test-Path -LiteralPath (Join-Path $output $name))) { throw "Missing failure evidence: $name" }
        }
        if ($report.checks.Count -ne 0 -or $report.scans.Count -ne 0) { throw 'Unverified executable was scanned or launched' }
        Write-Output "PASS: $expected saves evidence and stops before execution"
    }
} finally {
    $env:RUNNER_TEMP, $env:ProgramData, $env:ProgramFiles = $oldTemp, $oldData, $oldPrograms
    Remove-Variable diagnosticTestActive -Scope Global
    Remove-Item -LiteralPath $root -Force -Recurse
}
