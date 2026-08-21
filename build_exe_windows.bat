@echo off
setlocal
title Build KIPRIS Downloader EXE
set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
"%VENV_PY%" -m pip install --upgrade --only-binary=:all: pyinstaller==6.22.2
if errorlevel 1 goto build_error
"%VENV_PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name KIPRIS_Document_Hub --add-data "%APP_DIR%assets;assets" --add-data "%APP_DIR%web;web" --collect-all playwright --collect-all keyring --collect-all pymupdf --collect-all fastapi --collect-all uvicorn "%APP_DIR%launch_web.py"
if errorlevel 1 goto build_error
echo.
echo EXE created in the dist folder.
echo The target PC must have Microsoft Edge or Google Chrome installed.
pause
exit /b 0

:build_error
echo EXE build failed.
pause
exit /b 1
