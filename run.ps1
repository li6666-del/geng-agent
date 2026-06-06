# geng-agent project launcher (PowerShell).
#
# Why this exists: the default `python` on this machine (the harness venv) lacks
# numpy / scipy / matplotlib, which the generated reproduction projects need. Running
# geng-agent under it makes every numpy paper fall back to the local template. This
# launcher pins the project to a complete interpreter so reproductions actually run.
#
# Usage:  .\run.ps1 review paper.pdf --out case_001 --run-repro
# If blocked by execution policy:
#   powershell -ExecutionPolicy Bypass -File run.ps1 review paper.pdf --out case_001 --run-repro

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Interpreter: Python 3.11+ with numpy/scipy/matplotlib and geng_agent deps installed.
# If it moves, edit this one line.
$GengPython = 'D:\python\python.exe'

if (-not (Test-Path $GengPython)) {
    Write-Error "geng-agent interpreter not found: $GengPython - edit run.ps1 and point GengPython at a Python 3.11+ that has numpy, scipy, matplotlib."
    exit 1
}

& $GengPython -m geng_agent @args
exit $LASTEXITCODE
