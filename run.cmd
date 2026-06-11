@echo off
rem geng-agent project launcher (cmd). See run.ps1 for why this exists.
rem Usage:  run.cmd review paper.pdf --out case_001 --run-repro
setlocal
cd /d "%~dp0"

rem Interpreter: Python 3.11+ with numpy/scipy/matplotlib/torch and geng_agent deps.
rem Override before calling with: set "GENG_PYTHON=C:\path\to\python.exe"
if not defined GENG_PYTHON set "GENG_PYTHON=%USERPROFILE%\miniconda3\envs\torch\python.exe"

if not exist "%GENG_PYTHON%" (
  echo geng-agent interpreter not found: %GENG_PYTHON%
  echo Set GENG_PYTHON or point run.cmd at a Python 3.11+ with numpy/scipy/matplotlib/torch.
  exit /b 1
)

"%GENG_PYTHON%" -m geng_agent %*
