# Entwicklung

Die Anwendung wird während der Entwicklung direkt aus `src/` gestartet. PyInstaller wird nur für einen Release benötigt.

## Einmalige Einrichtung

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Command setup
```

Alternativ kann `setup_windows.bat` verwendet werden. Die Entwicklungsumgebung liegt in `.venv`; die editierbare Entwicklungsinstallation (`pip install -e '.[dev]'`) enthält Docling, das über `USE_DOCLING=true` aktiviert werden kann.

## Entwickeln und testen

GUI mit sichtbarem Log starten:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Command run
```

Oder `run_gui.bat` ausführen. Für einen versteckten Start bleibt `run_gui.vbs` verfügbar.

Tests und Syntaxprüfung:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Command check
```

Nur Tests ausführen:

```powershell
run_tests.bat
```

In VS Code stehen zusätzlich die Startkonfiguration **Enargeia GUI (Quellcode)** und die Testkonfiguration für die aktuelle Datei zur Verfügung.

Änderungen an Python-Dateien werden nach einem Neustart der Quellanwendung verwendet. Ein PyInstaller-Build ist dafür nicht erforderlich.

## Release bauen

Ein Release wird ausdrücklich über diesen Befehl gebaut:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\build_windows.ps1
```

Der Release-Build verwendet die getrennte Umgebung `.venv-packaging`, installiert die Paketierungskomponenten dort und schreibt die Artefakte nach `dist/`. Die Entwicklungsumgebung `.venv` und der Quellcode bleiben davon getrennt.
