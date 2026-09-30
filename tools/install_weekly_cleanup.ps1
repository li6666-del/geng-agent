param(
    [Parameter(Mandatory = $true)][string]$Config,
    [string]$TaskName = 'GengAgent-WeeklyCleanup'
)

$ErrorActionPreference = 'Stop'
$ConfigPath = (Resolve-Path -LiteralPath $Config).Path
$StartupScript = Join-Path $PSScriptRoot 'start_local_web_worker.ps1'
# The fixed action contains paths only, never worker credentials. No immediate
# execution: the first scheduled run applies the seven-day server policy.
$Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $StartupScript + '" -Config "' + $ConfigPath + '" -CleanupOnly'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '04:10'
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Description '每周清理完成至少七天的网站交付包对应本地实验现场；运行中的论文由常驻接单程序保护。' -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Write-Output ('已安装每周一 04:10 清理任务：' + $TaskName + '；错过执行时间将在登录后补跑。')
