@echo off
setlocal
set "APP_DIR=%~dp0"
set "VENV_PYW=%APP_DIR%.venv\Scripts\pythonw.exe"

if not exist "%VENV_PYW%" (
  echo The program is not installed yet.
  echo Run install_windows.bat first.
  pause
  exit /b 1
)

wscript.exe "%APP_DIR%KIPRIS_Document_Hub.vbs"
exit /b 0
