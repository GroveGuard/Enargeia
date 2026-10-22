@echo off
setlocal
cd /d "%~dp0"
rem Development launcher: run the editable source tree with visible logs.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" -Command run
exit /b %errorlevel%
