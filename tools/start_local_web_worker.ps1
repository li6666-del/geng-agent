param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$Once
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
if ($Once) { $WorkerArguments += '--once' }
Push-Location -LiteralPath $ProjectRoot
try {
    & $WorkerPython @WorkerArguments
    $WorkerExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $WorkerExitCode
