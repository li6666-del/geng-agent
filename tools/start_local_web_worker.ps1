param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$Once,
    [switch]$CleanupOnly,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ConfigPath = (Resolve-Path -LiteralPath $Config).Path
$DefaultWorkerPython = Join-Path $env:USERPROFILE 'Desktop\耿同学agent_cases\observed_rayleigh_20260908\venv\Scripts\python.exe'
$WorkerPython = if ($env:GENG_PYTHON) { $env:GENG_PYTHON } else { $DefaultWorkerPython }
if (-not (Test-Path -LiteralPath $WorkerPython -PathType Leaf)) {
    throw 'Set GENG_PYTHON to an existing Python 3.11+ interpreter with geng-agent[web] installed.'
}
$WorkerArguments = @('-m', 'geng_agent.web.local_worker', '--config', $ConfigPath)
if ($CleanupOnly) {
    $WorkerArguments = @('-m', 'geng_agent.web.local_retention', '--config', $ConfigPath)
    if (-not $DryRun) { $WorkerArguments += '--apply' }
} elseif ($Once) { $WorkerArguments += '--once' }
Push-Location -LiteralPath $ProjectRoot
try {
    if ($CleanupOnly) {
        $LogDirectory = 'D:\geng-artifacts\logs'
        New-Item -Path $LogDirectory -ItemType Directory -Force | Out-Null
        $CleanupLog = Join-Path $LogDirectory ('online-cleanup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 6)) + '.log')
        & $WorkerPython @WorkerArguments *> $CleanupLog
    } else {
        & $WorkerPython @WorkerArguments
    }
    $WorkerExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $WorkerExitCode
