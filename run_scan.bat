@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Verwendung: run_scan.bat "C:\Pfad\zum\Verzeichnis" [weitere win-kg scan Optionen]
  echo Beispiel: run_scan.bat "C:\MS_VSCode_Workspace\Fletapps_HA" --extract-entities
  exit /b 2
)
if not exist ".venv\Scripts\win-kg.exe" (
  echo Projekt noch nicht installiert. Zuerst setup_windows.bat ausfuehren.
  exit /b 2
)
.venv\Scripts\win-kg.exe scan %*
endlocal
