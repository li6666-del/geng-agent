[CmdletBinding()]
param([ValidateSet('Install', 'Status', 'Uninstall')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'
$TaskName = 'GengAgent-WriterBoundaryStartup'
$TaskDescription = 'GengAgent fixed Writer AppContainer volume-query permissions v1'
$SystemDirectory = [Environment]::GetFolderPath('System')
$ProgramDirectory = [Environment]::GetFolderPath('ProgramFiles')
$InstallDirectory = Join-Path $ProgramDirectory 'GengAgentWriterBoundary'
$Executable = Join-Path $InstallDirectory 'WriterVolumeQuery.exe'
$LatestRecord = Join-Path $InstallDirectory 'logs\latest.json'

function Assert-NoReparse([string]$Path) {
    $item = [IO.Path]::GetFullPath($Path)
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing an installation path containing a reparse point: $item"
            }
        }
        $item = [IO.Path]::GetDirectoryName($item)
    }
}

function Get-TaskState {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    $info = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\' }
    $latest = if (Test-Path -LiteralPath $LatestRecord) { Get-Content -LiteralPath $LatestRecord -Raw | ConvertFrom-Json }
    [pscustomobject]@{TaskName=$TaskName; Installed=[bool]$task; State=if($task){[string]$task.State}else{'Absent'};
        Executable=$Executable; Principal=if($task){$task.Principal.UserId}else{$null};
        LastRunTime=if($info){$info.LastRunTime.ToString('o')}else{$null};
        LastTaskResult=if($info){$info.LastTaskResult}else{$null}; Latest=$latest}
}

if ($Action -eq 'Status') { Get-TaskState | ConvertTo-Json -Depth 8; exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Install/Uninstall requires one explicit Administrator confirmation. No task was changed.'
}
Assert-NoReparse $InstallDirectory
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and ($existing.Description -ne $TaskDescription -or $existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $Executable)) {
    throw 'An unrelated task has the same name; it was left unchanged.'
}
if ($Action -eq 'Uninstall') {
    if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false }
    # Keep logs and the protected helper for investigation; no recursive delete.
    # This stops future grants. Current-boot ACLs can be reverted separately.
    Get-TaskState | ConvertTo-Json -Depth 8
    exit 0
}
if ($existing -and $existing.State -eq 'Running') { throw 'The fixed setup task is running; retry installation after it finishes.' }

$source = Join-Path $PSScriptRoot 'windows\WriterVolumeQuery.cs'
$sourceText = [IO.File]::ReadAllText($source)
$protectedSddl = 'O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)'
$directoryAcl = New-Object Security.AccessControl.DirectorySecurity
$directoryAcl.SetSecurityDescriptorSddlForm($protectedSddl)
if (!(Test-Path -LiteralPath $InstallDirectory)) {
    [IO.Directory]::CreateDirectory($InstallDirectory, $directoryAcl) | Out-Null
} else {
    # Do not adopt an existing user-owned directory for a SYSTEM program.
    $existingAcl = Get-Acl -LiteralPath $InstallDirectory
    $owner = $existingAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @('S-1-5-18','S-1-5-32-544')) { throw 'Existing install directory is not administrator-owned.' }
    $existingAcl.SetSecurityDescriptorSddlForm($protectedSddl)
    Set-Acl -LiteralPath $InstallDirectory -AclObject $existingAcl
}
foreach ($item in @(Get-ChildItem -LiteralPath $InstallDirectory -Force -Recurse)) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'A protected installation item is a reparse point.' }
    $acl = if ($item.PSIsContainer) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1200a9;;;BU)')
    if ($item.PSIsContainer) { $acl.SetSecurityDescriptorSddlForm($protectedSddl) }
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}
# Compile in the protected directory, using the Windows-provided .NET runtime.
# No shared/user Python, project script, profile, env file or network dependency
# is executed by the scheduled task.
$buildDirectory = Join-Path $InstallDirectory ('build-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($buildDirectory, $directoryAcl) | Out-Null
$priorTemp = $env:TEMP; $priorTmp = $env:TMP
try {
    $env:TEMP=$buildDirectory; $env:TMP=$buildDirectory
    $candidate = Join-Path $buildDirectory 'WriterVolumeQuery.exe'
    Add-Type -TypeDefinition $sourceText -OutputAssembly $candidate -OutputType ConsoleApplication -ReferencedAssemblies @('System.dll','System.Core.dll','System.Web.Extensions.dll')
    if (Test-Path -LiteralPath $Executable) {
        $backup=Join-Path $InstallDirectory ('WriterVolumeQuery-' + [guid]::NewGuid().ToString('N') + '.previous.exe')
        [IO.File]::Replace($candidate,$Executable,$backup)
    } else { [IO.File]::Move($candidate,$Executable) }
} finally { $env:TEMP=$priorTemp; $env:TMP=$priorTmp }

$taskAction = New-ScheduledTaskAction -Execute $Executable -WorkingDirectory $InstallDirectory
$trigger = New-ScheduledTaskTrigger -AtStartup
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Description $TaskDescription -Action $taskAction -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force | Out-Null
# Administrator-only modification/execution; ordinary users can inspect status.
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$registered = $scheduler.GetFolder('\').GetTask($TaskName)
$registered.SetSecurityDescriptor('D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)',0)
$installedAt=Get-Date
Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
$deadline=$installedAt.AddSeconds(40)
do {
    Start-Sleep -Milliseconds 250
    $state=Get-TaskState
    $fresh = $state.Latest -and ([datetimeoffset]$state.Latest.time).LocalDateTime -ge $installedAt
    if ($fresh -and $state.State -ne 'Running') { break }
} while ((Get-Date) -lt $deadline)
$state | ConvertTo-Json -Depth 8
if (!$fresh -or !$state.Latest.success -or $state.LastTaskResult -ne 0) { throw 'Installed, but the initial scheduled permission restore did not succeed. See the protected latest.json.' }
