# Runnable with pwsh; no real Defender configuration or executable is changed.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('zm-audit-test-' + [guid]::NewGuid())
$audit = Join-Path $PSScriptRoot '../scripts/audit-windows-release.ps1'
$oldData = $env:ProgramData
$env:ProgramData = $root
$global:auditTestMode = ''
$global:auditTestScans = 0
function Get-MpComputerStatus { [pscustomobject]@{ AntivirusEnabled = $global:auditTestMode -ne 'unavailable'; AMServiceEnabled = $true; RealTimeProtectionEnabled = $false; AntivirusSignatureVersion = 'test'; AMEngineVersion = 'test' } }
function Get-MpPreference { [pscustomobject]@{ ExclusionPath = @(); ExclusionExtension = @(); ExclusionProcess = @() } }
function Update-MpSignature { }
function Get-MpThreatDetection { @() }
function Get-AuthenticodeSignature { [pscustomobject]@{ Status = 'NotSigned'; SignerCertificate = $null } }
function Get-ChildItem {
    [CmdletBinding()]param([string]$LiteralPath, [switch]$Recurse, [switch]$File, [string]$Filter)
    if ($Filter -eq 'MpCmdRun.exe') { return [pscustomobject]@{ FullName = 'Invoke-AuditTestScanner' } }
    Microsoft.PowerShell.Management\Get-ChildItem @PSBoundParameters
}
function Invoke-AuditTestScanner {
    param([switch]$CheckExclusion, [string]$Path, [switch]$Scan, [int]$ScanType, [string]$File)
    if ($CheckExclusion) {
        $global:LASTEXITCODE = 1
        if ($global:auditTestMode -eq 'excluded' -and $Path.EndsWith('.exe')) { $global:LASTEXITCODE = 0 }
        if ($global:auditTestMode -eq 'script_excluded' -and $Path.EndsWith('.ps1')) { $global:LASTEXITCODE = 0 }
        if ($global:auditTestMode -eq 'unknown') { $global:LASTEXITCODE = 5 }
    } else {
        $global:auditTestScans++
        $global:LASTEXITCODE = 0
        if ($global:auditTestMode -eq 'quarantined' -and (Test-Path -LiteralPath $File -PathType Container)) {
            Remove-Item -LiteralPath (Join-Path $File 'ZM AI TOOL.exe')
        }
    }
    'Test scanner only'
}
try {
    foreach ($mode in @('excluded', 'script_excluded', 'unknown', 'unavailable', 'quarantined', 'passed')) {
        $global:auditTestMode = $mode
        $global:auditTestScans = 0
        $bundle = Join-Path $root "$mode/bundle"
        $output = Join-Path $root "$mode/reports"
        New-Item -ItemType Directory -Force $bundle, $output | Out-Null
        $exe = Join-Path $bundle 'ZM AI TOOL.exe'
        $setup = Join-Path $root "$mode/Setup.exe"
        Set-Content -LiteralPath $exe 'not executable; audit fixture'
        Set-Content -LiteralPath $setup 'not executable; audit fixture'
        Set-Content -LiteralPath (Join-Path $bundle 'worker.ps1') '# not executed'
        @{ files = @(@{ path = 'ZM AI TOOL.exe'; sha256 = (Get-FileHash $exe).Hash.ToLowerInvariant() }); assets = @(@{ name = 'Setup.exe'; sha256 = (Get-FileHash $setup).Hash.ToLowerInvariant() }) } |
            ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output 'windows-artifact-manifest.json')
        $failed = $false
        try { & $audit -Bundle $bundle -Installer $setup -Output $output } catch { $failed = $true }
        $expected = if ($mode -in @('excluded', 'script_excluded')) { 'scan_excluded' } elseif ($mode -eq 'passed') { 'passed' } else { 'failed' }
        $report = Get-Content (Join-Path $output 'windows-defender-report.json') -Raw | ConvertFrom-Json
        if ($report.status -ne $expected -or $failed -ne ($mode -ne 'passed')) { throw "Incorrect audit verdict for ${mode}: $($report | ConvertTo-Json -Compress -Depth 5)" }
        if ($mode -notin @('passed', 'quarantined') -and $global:auditTestScans -ne 0) { throw 'Scanner ran before coverage/service validation' }
        if ($mode -eq 'passed' -and ($global:auditTestScans -ne 2 -or $report.realTimeProtectionEnabled)) { throw 'Incorrect scan/protection coverage' }
        Write-Output "PASS: $mode"
    }
    # Invalid worker parameters must fail during binding, before Windows Update or callbacks.
    $worker = Join-Path $PSScriptRoot '../backend/pipeline/core/nvidia_driver_update.ps1'
    foreach ($invalid in @(@{ Callback = 'https://example.com/progress' }, @{ Token = 'bad' }, @{ OperationId = 'bad' }, @{ AcceptedFingerprint = 'bad' })) {
        $params = @{ Callback = 'http://127.0.0.1:12345/progress'; Token = 'a' * 43; OperationId = [guid]::NewGuid() }
        foreach ($key in $invalid.Keys) { $params[$key] = $invalid[$key] }
        $blocked = $false
        try { & $worker @params } catch [System.Management.Automation.ParameterBindingException] { $blocked = $true }
        if (-not $blocked) { throw 'Worker accepted invalid parameters' }
    }
    Write-Output 'PASS: driver worker rejects invalid parameters before execution'
} finally {
    $env:ProgramData = $oldData
    Remove-Variable auditTestMode, auditTestScans -Scope Global
    Remove-Item -LiteralPath $root -Recurse -Force
}
