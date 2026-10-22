@echo off
setlocal
cd /d "%~dp0"
title Enargeia - Developer Setup

echo === Enargeia - Entwicklungsumgebung ===
echo Diese Datei richtet die Quellcode-Umgebung fuer Entwickler ein.
echo Fuer Endanwender ist der Windows-Installer unter packaging\windows vorgesehen.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" -Command setup
if errorlevel 1 goto error

echo.
echo Entwicklungsumgebung bereit.
echo Starte run_gui.bat fuer Entwicklung mit sichtbarem Log.
echo Starte run_gui.vbs fuer einen versteckten Start.
echo Die Anwendung legt ihre Konfiguration standardmaessig in %%LOCALAPPDATA%%\Enargeia ab.
echo.
pause
exit /b 0
:error
echo.
echo Setup fehlgeschlagen. Die Fehlermeldung steht oben.
pause
exit /b 1
