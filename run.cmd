@echo off
rem geng-agent project launcher (cmd). See run.ps1 for why this exists.
rem Usage:  run.cmd review paper.pdf --out case_001 --run-repro
setlocal
cd /d "%~dp0"

rem Interpreter: Python 3.11+ with numpy/scipy/matplotlib and geng_agent deps. Edit if it moves.
set "GENG_PYTHON=D:\python\python.exe"

if not exist "%GENG_PYTHON%" (
  echo geng-agent interpreter not found: %GENG_PYTHON%
  echo Edit run.cmd and point GENG_PYTHON at a Python 3.11+ with numpy/scipy/matplotlib.
  exit /b 1
)

"%GENG_PYTHON%" -m geng_agent %*
