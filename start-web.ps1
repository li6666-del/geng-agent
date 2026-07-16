param(
    [string]$HostAddress = '127.0.0.1',
    [int]$Port = 8765,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$DefaultPython = Join-Path $env:USERPROFILE 'miniconda3\envs\torch\python.exe'
$Python = if ($env:GENG_PYTHON) { $env:GENG_PYTHON } else { $DefaultPython }
if (-not (Test-Path -LiteralPath $Python)) {
    throw "geng-agent interpreter not found: $Python"
}

$CasesRoot = if ($env:GENG_CASES_ROOT) {
    $env:GENG_CASES_ROOT
} else {
    Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'geng_cases'
}
$RuntimeRoot = Join-Path $CasesRoot '.web'
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$PidPath = Join-Path $RuntimeRoot 'server.pid'
$StdoutPath = Join-Path $RuntimeRoot 'server.stdout.log'
$StderrPath = Join-Path $RuntimeRoot 'server.stderr.log'

if (Test-Path -LiteralPath $PidPath) {
    $ExistingPid = [int](Get-Content -LiteralPath $PidPath -Raw)
    $Existing = Get-CimInstance Win32_Process -Filter "ProcessId = $ExistingPid" -ErrorAction SilentlyContinue
    if ($Existing -and $Existing.CommandLine -match 'geng_agent\.web') {
        Write-Output "Web service is already running: http://${HostAddress}:$Port (PID $ExistingPid)"
        exit 0
    }
    Remove-Item -LiteralPath $PidPath -Force
}

$Arguments = @('-m', 'geng_agent.web', '--host', $HostAddress, '--port', "$Port")
if ($Foreground) {
    & $Python @Arguments
    exit $LASTEXITCODE
}

$StartParameters = @{
    FilePath = $Python
    ArgumentList = $Arguments
    WorkingDirectory = $PSScriptRoot
    WindowStyle = 'Hidden'
    RedirectStandardOutput = $StdoutPath
    RedirectStandardError = $StderrPath
    PassThru = $true
}
$Process = Start-Process @StartParameters
Set-Content -LiteralPath $PidPath -Value $Process.Id -Encoding ascii

for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
        throw "Web service exited during startup. See $StderrPath"
    }
    try {
        $Health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/api/v1/health" -TimeoutSec 2
        if ($Health.ok) {
            Write-Output "Web service started without a lifetime limit: http://${HostAddress}:$Port (PID $($Process.Id))"
            Write-Output "Logs: $StdoutPath ; $StderrPath"
            exit 0
        }
    } catch {
        # The health endpoint may not be ready yet.
    }
}

throw "Web service did not become healthy. See $StderrPath"
