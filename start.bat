@echo off
rem Double-click to start Canvas til lyd and open it in the browser.
rem Closing this window stops the app. Extra arguments go to the launcher,
rem for example:  start.bat --port 8001
setlocal
cd /d "%~dp0"
title Canvas til lyd

if not exist ".venv\Scripts\python.exe" (
  echo Fant ikke .venv\Scripts\python.exe i %CD%
  echo Folg "Installasjon" i README.md for a lage det virtuelle miljoet.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m app.launcher %*
if errorlevel 1 pause
endlocal
