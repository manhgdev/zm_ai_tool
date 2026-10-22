# Fixed, bundled worker. No downloaded script, command, executable or path is accepted.
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^http://127\.0\.0\.1:[1-9][0-9]{0,4}/progress$')][string]$Callback,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_-]{43}$')][string]$Token,
    [Parameter(Mandatory=$true)][guid]$OperationId,
    [ValidatePattern('^(?:[0-9a-f]{64})?$')][string]$AcceptedFingerprint = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$config = @{ callback = $Callback; token = $Token; operationId = $OperationId.ToString(); acceptedFingerprint = $AcceptedFingerprint }
$mutex = $null
$held = $false
function Send-State($phase, $extra = @{}) {
    $body = @{ phase = $phase; operationId = $config.operationId }
    foreach ($key in $extra.Keys) { $body[$key] = $extra[$key] }
    $json = ConvertTo-Json -InputObject $body -Depth 12 -Compress
    # If the UI/backend disappears, do not kill an ongoing Windows installation.
    # A failed callback before installation, however, prevents starting it.
    Invoke-RestMethod -Uri $config.callback -Method Post -Headers @{ 'X-Driver-Token' = $config.token } -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 10 | Out-Null
}
function Find-Drivers($session) {
    $devices = @(Get-PnpDevice -Class Display -PresentOnly | Where-Object { $_.InstanceId -match '^PCI\\VEN_10DE&' })
    if (!$devices.Count) { throw 'NVIDIA_DEVICE_NOT_FOUND' }
    $inventory = @()
    foreach ($device in $devices) {
        $ids = @((Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName 'DEVPKEY_Device_HardwareIds').Data)
        $ids += @((Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName 'DEVPKEY_Device_CompatibleIds').Data)
        $inventory += @{ name = $device.FriendlyName; deviceInstanceId = $device.InstanceId; hardwareIds = $ids; deviceStatus = [string]$device.Status }
    }
    $searcher = $session.CreateUpdateSearcher()
    # Respect the configured update service / enterprise policy. Include optional drivers.
    $search = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Driver'")
    if ([int]$search.ResultCode -ne 2) { throw 'DRIVER_SEARCH_FAILED' }
    $assignedSearch = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Driver' and IsAssigned=1")
    if ([int]$assignedSearch.ResultCode -ne 2) { throw 'DRIVER_SEARCH_FAILED' }
    $assignedIds = @($assignedSearch.Updates | ForEach-Object { $_.Identity.UpdateID })
    $selected = @{}
    foreach ($device in $inventory) {
        $candidates = @()
        foreach ($update in $search.Updates) {
            if ($update.DriverClass -ine 'Display' -or $update.DriverProvider -notmatch '^NVIDIA(?:\s|$)') { continue }
            $id = [string]$update.DriverHardwareID
            if ($id -notmatch '^PCI\\VEN_10DE&' -or $device.hardwareIds -inotcontains $id) { continue }
            $candidates += [pscustomobject]@{ update = $update; assigned = [int]($assignedIds -contains $update.Identity.UpdateID); specificity = $id.Length; date = $update.DriverVerDate }
        }
        $best = $candidates | Sort-Object -Property @{Expression='assigned';Descending=$true}, @{Expression='specificity';Descending=$true}, @{Expression='date';Descending=$true}, @{Expression={$_.update.Identity.UpdateID};Descending=$false} | Select-Object -First 1
        if ($best) { $selected[$best.update.Identity.UpdateID] = $best.update }
    }
    if (!$selected.Count) { throw 'DRIVER_UPDATE_NOT_FOUND' }
    $collection = New-Object -ComObject Microsoft.Update.UpdateColl
    $terms = @()
    $identities = @()
    $fingerprint = ''
    foreach ($id in @($selected.Keys | Sort-Object)) {
        $update = $selected[$id]
        [void]$collection.Add($update)
        $identities += @{ id = $id; revision = $update.Identity.RevisionNumber; hardwareId = $update.DriverHardwareID; title = $update.Title }
        $fingerprint += "$id|$($update.Identity.RevisionNumber)|$($update.DriverHardwareID)|$($update.EulaText)`n"
        if (!$update.EulaAccepted -and $update.EulaText) { $terms += [string]$update.EulaText }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($fingerprint))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return @{ updates = $collection; identities = $identities; terms = $terms; fingerprint = $hash; inventory = $inventory }
}
try {
    $mutex = [System.Threading.Mutex]::new($false, 'Global\ZMAIToolNvidiaDriver')
    try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (!$held) { throw 'RUNTIME_BUSY' }
    Send-State 'driver_search'
    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'ZM AI TOOL NVIDIA driver repair'
    $found = Find-Drivers $session
    if ($config.acceptedFingerprint -and $config.acceptedFingerprint -cne $found.fingerprint) { throw 'DRIVER_CANDIDATE_CHANGED' }
    if ($found.terms.Count -and $config.acceptedFingerprint -cne $found.fingerprint) {
        Send-State 'driver_eula' @{ eula = $found.terms; fingerprint = $found.fingerprint; updates = $found.identities; gpus = $found.inventory }
        exit 0
    }
    $installer = $session.CreateUpdateInstaller()
    if ($installer.IsBusy) { throw 'DRIVER_INSTALL_BUSY' }
    if ($installer.RebootRequiredBeforeInstallation) {
        Send-State 'driver_reboot' @{ rebootRequired = $true; updates = $found.identities }
        exit 0
    }
    foreach ($update in $found.updates) {
        if (!$update.EulaAccepted) { $update.AcceptEula() }
    }
    Send-State 'driver_download' @{ updates = $found.identities; gpus = $found.inventory }
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $found.updates
    $download = $downloader.Download()
    if ([int]$download.ResultCode -ne 2) { throw "DRIVER_DOWNLOAD_FAILED: $($download.HResult)" }
    foreach ($update in $found.updates) { if (!$update.IsDownloaded) { throw 'DRIVER_DOWNLOAD_FAILED' } }
    # Hardware, policy and candidate identities must still match before installation.
    $fresh = Find-Drivers $session
    if ($fresh.fingerprint -cne $found.fingerprint) { throw 'DRIVER_CANDIDATE_CHANGED' }
    Send-State 'driver_install' @{ updates = $found.identities }
    $installer.Updates = $found.updates
    $installer.AllowSourcePrompts = $false
    $installer.ForceQuiet = $true
    $result = $installer.Install()
    $results = @()
    $failed = $false
    for ($i = 0; $i -lt $found.updates.Count; $i++) {
        $item = $result.GetUpdateResult($i)
        $results += @{ id = $found.updates.Item($i).Identity.UpdateID; resultCode = [int]$item.ResultCode; hresult = $item.HResult; rebootRequired = $item.RebootRequired }
        if ([int]$item.ResultCode -ne 2) { $failed = $true }
    }
    if ($result.RebootRequired) {
        Send-State 'driver_reboot' @{ rebootRequired = $true; results = $results; partialFailure = $failed }
    } elseif ($failed -or [int]$result.ResultCode -ne 2) {
        Send-State 'driver_failed' @{ errorCode = 'DRIVER_INSTALL_FAILED'; results = $results }
    } else {
        Send-State 'driver_verify' @{ results = $results }
    }
} catch {
    $detail = [string]$_.Exception.Message
    $code = ($detail -split ':')[0]
    $known = @('NVIDIA_DEVICE_NOT_FOUND', 'DRIVER_UPDATE_NOT_FOUND', 'DRIVER_SEARCH_FAILED', 'DRIVER_DOWNLOAD_FAILED', 'DRIVER_INSTALL_BUSY', 'DRIVER_CANDIDATE_CHANGED', 'RUNTIME_BUSY')
    if ($known -notcontains $code) { $code = 'DRIVER_UPDATE_FAILED' }
    try { Send-State 'driver_failed' @{ errorCode = $code; diagnostics = $detail } } catch { }
    exit 1
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
