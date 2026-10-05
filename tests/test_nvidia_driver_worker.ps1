# Non-mutating worker tests, Windows PowerShell 5.1+; no WUA download/install or UAC.
# powershell.exe -NoProfile -File tests/test_nvidia_driver_worker.ps1
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '../backend/pipeline/core/nvidia_driver_update.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $source), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Find-Drivers' }, $true)
if (!$function) { throw 'Missing production Find-Drivers function' }
Invoke-Expression $function.Extent.Text

function Assert-True($value, $message) { if (!$value) { throw $message } }
function Get-PnpDevice { param($Class, [switch]$PresentOnly)
    return $script:devices
}
function Get-PnpDeviceProperty { param($InstanceId, $KeyName)
    return @{ Data = @('PCI\VEN_10DE&DEV_1234&SUBSYS_5678', 'PCI\VEN_10DE&DEV_1234') }
}
function New-Object { param($ComObject)
    if ($ComObject -ne 'Microsoft.Update.UpdateColl') { throw 'Unexpected COM object in selection test' }
    return ,([System.Collections.ArrayList]::new())
}
function Update($id, $hardware = 'PCI\VEN_10DE&DEV_1234', $provider = 'NVIDIA', $class = 'Display', $date = '2025-01-01') {
    return [pscustomobject]@{
        Identity = @{ UpdateID = $id; RevisionNumber = 1 }
        DriverHardwareID = $hardware; DriverProvider = $provider; DriverClass = $class
        DriverVerDate = [datetime]$date; Title = $id; EulaText = 'test terms'; EulaAccepted = $false
    }
}
$script:devices = @([pscustomobject]@{ InstanceId = 'PCI\VEN_10DE&DEV_1234\1'; FriendlyName = 'Test NVIDIA'; Status = 'OK' })
$script:assigned = @()
$script:updates = @()
$script:searchCode = 2
$searcher = [pscustomobject]@{}
$searcher | Add-Member ScriptMethod Search {
    param($criteria)
    $items = if ($criteria -match 'IsAssigned=1') { $script:assigned } else { $script:updates }
    return @{ ResultCode = $script:searchCode; Updates = @($items) }
}
$session = [pscustomobject]@{ Searcher = $searcher }
$session | Add-Member ScriptMethod CreateUpdateSearcher { return $this.Searcher }

$script:updates = @((Update 'intel' -provider 'Intel'), (Update 'audio' -class 'MEDIA'), (Update 'wrong' -hardware 'PCI\VEN_10DE&DEV_9999'), (Update 'good'))
$found = Find-Drivers $session
Assert-True ($found.identities.Count -eq 1 -and $found.identities[0].id -eq 'good') 'Must filter provider, class and hardware'
Assert-True ($found.terms.Count -eq 1) 'Must present unaccepted EULA'

$script:updates = @((Update 'generic'), (Update 'specific' -hardware 'PCI\VEN_10DE&DEV_1234&SUBSYS_5678'))
Assert-True ((Find-Drivers $session).identities[0].id -eq 'specific') 'Prefer specific hardware ID for optional drivers'
$script:assigned = @($script:updates[0])
Assert-True ((Find-Drivers $session).identities[0].id -eq 'generic') 'Prefer Windows assigned driver'
$script:assigned = @()
$script:updates = @((Update 'older'), (Update 'newer' -date '2026-01-01'))
$found = Find-Drivers $session
Assert-True ($found.identities[0].id -eq 'newer') 'Prefer newest date at the same hardware specificity'
$fingerprint = $found.fingerprint
$script:updates[1].Identity.RevisionNumber = 2
Assert-True ((Find-Drivers $session).fingerprint -ne $fingerprint) 'Reject changed candidate revision'
$fingerprint = (Find-Drivers $session).fingerprint
$script:updates[1].EulaText = 'changed terms'
Assert-True ((Find-Drivers $session).fingerprint -ne $fingerprint) 'Bind consent to exact terms'
$script:devices += $script:devices[0]
Assert-True ((Find-Drivers $session).identities.Count -eq 1) 'Deduplicate shared GPU driver packages'
$script:updates = @()
try { Find-Drivers $session; throw 'Expected missing driver failure' }
catch { Assert-True ($_.Exception.Message -eq 'DRIVER_UPDATE_NOT_FOUND') 'Missing candidate must fail safely' }
$script:searchCode = 3
try { Find-Drivers $session; throw 'Expected policy/search failure' }
catch { Assert-True ($_.Exception.Message -eq 'DRIVER_SEARCH_FAILED') 'Partial or failed search must not install' }
Write-Output 'PASS: worker syntax, hardware matching, ranking, deduplication, EULA fingerprint and failed search'
