# geng-agent project launcher (PowerShell).
#
# Why this exists: the default `python` on this machine (the harness venv) lacks
# the scientific/GPU stack that generated reproduction projects need. Running
# geng-agent under it makes dependency policy prompts and guarded execution see
# the wrong environment. This launcher pins the project to the torch/CUDA env.
#
# Usage:  .\run.ps1 review paper.pdf --out case_001 --run-repro
# If blocked by execution policy:
#   powershell -ExecutionPolicy Bypass -File run.ps1 review paper.pdf --out case_001 --run-repro

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Interpreter: Python 3.11+ with numpy/scipy/matplotlib/torch and geng_agent deps.
# Override temporarily with: $env:GENG_PYTHON='C:\path\to\python.exe'
$DefaultGengPython = Join-Path $env:USERPROFILE 'miniconda3\envs\torch\python.exe'
$GengPython = if ($env:GENG_PYTHON) { $env:GENG_PYTHON } else { $DefaultGengPython }

if (-not (Test-Path $GengPython)) {
    Write-Error "geng-agent interpreter not found: $GengPython - set GENG_PYTHON or point run.ps1 at a Python 3.11+ that has numpy, scipy, matplotlib, torch."
    exit 1
}

& $GengPython -m geng_agent @args
exit $LASTEXITCODE
