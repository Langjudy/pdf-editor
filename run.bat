@echo off
cd /d "%~dp0"
start "" "%~dp0venv\pythonw.exe" -m pdfedit %*
