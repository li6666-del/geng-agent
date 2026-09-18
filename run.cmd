@echo off
rem Use the same interpreter selection as run.ps1, including Unicode paths.
rem Override before calling with: set "GENG_PYTHON=C:\path\to\python.exe"
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
