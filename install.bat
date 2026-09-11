@echo off
rem First-time setup: double-click to create .venv and install Canvas til lyd.
rem Needs Python 3.11 or newer from https://www.python.org/downloads/
rem No PowerShell execution policy change is needed. Extra arguments go to
rem scripts\install.py, for example:  install.bat --dev   or   install.bat --no-shortcut
rem cmd.exe reads .bat files in the console code page, so keep this file ASCII-only.
setlocal
cd /d "%~dp0"
title Canvas til lyd - installasjon

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (python -c "import sys" >nul 2>nul && set "PY=python")
if not defined PY (
  echo Fant ikke Python.
  echo Installer Python 3.11 eller nyere fra https://www.python.org/downloads/
  echo Huk av for "Add python.exe to PATH" under installasjonen, og start install.bat igjen.
  pause
  exit /b 1
)

%PY% scripts\install.py %*
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
