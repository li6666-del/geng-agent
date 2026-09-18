# geng-agent project launcher (PowerShell).
#
# Use the venv prepared for the observed Rayleigh paper run by default.
# GENG_PYTHON can select another existing interpreter for a future run.
#
# Usage:  .\run.ps1 review paper.pdf --out case_001 --run-repro
# Relative case names are stored under %USERPROFILE%\Desktop\耿同学agent_cases.
# If blocked by execution policy:
#   powershell -ExecutionPolicy Bypass -File run.ps1 review paper.pdf --out case_001 --run-repro

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Interpreter: the existing Python 3.12 venv with project and numerical dependencies.
# Override temporarily with: $env:GENG_PYTHON='C:\path\to\python.exe'
$DefaultGengPython = Join-Path $env:USERPROFILE 'Desktop\耿同学agent_cases\observed_rayleigh_20260908\venv\Scripts\python.exe'
$GengPython = if ($env:GENG_PYTHON) { $env:GENG_PYTHON } else { $DefaultGengPython }

if (-not (Test-Path -LiteralPath $GengPython -PathType Leaf)) {
    Write-Error "geng-agent interpreter not found: $GengPython - set GENG_PYTHON to an existing Python 3.11+ with the project dependencies."
    exit 1
}

$env:GENG_PYTHON = $GengPython
& $GengPython -m geng_agent @args
exit $LASTEXITCODE
