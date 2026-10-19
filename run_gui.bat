@echo off
rem Forward to the hidden Windows Script Host launcher and close this window.
start "" "%SystemRoot%\System32\wscript.exe" //B //NoLogo "%~dp0run_gui.vbs"
exit /b 0
