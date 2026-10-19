@echo off
setlocal
cd /d "%~dp0"
title Windows Knowledge Graph - Setup

echo === Windows Knowledge Graph Setup ===
echo.
where python >nul 2>nul
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" (
  echo Erstelle virtuelle Python-Umgebung...
  python -m venv .venv
  if errorlevel 1 goto error
)

echo Aktualisiere pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto error

echo Installiere Projekt und Abhaengigkeiten...
".venv\Scripts\python.exe" -m pip install -e ".[gui,mcp,test]"
if errorlevel 1 goto error

if not exist ".env" copy ".env.example" ".env"
if errorlevel 1 goto error

echo.
echo Installation abgeschlossen.
echo Bearbeite .env und trage dein Neo4j-Passwort ein.
echo Danach kannst du run_gui.vbs doppelklicken (oder run_gui.bat verwenden).
echo.
pause
exit /b 0

:no_python
echo FEHLER: Python wurde nicht gefunden. Installiere Python 3.10 oder neuer und pruefe PATH.
:error
echo.
echo Setup fehlgeschlagen. Die Fehlermeldung steht oben.
pause
exit /b 1
