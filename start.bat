@echo off
cd /d "%~dp0"
rem Log-Fenster von pyMHF startet 'python' aus dem PATH: venv zuerst, sonst wird Python 3.14 ohne pymhf genommen
set "PATH=%~dp0.venv\Scripts;%PATH%"
".venv\Scripts\pymhf.exe" run nmspy
pause
