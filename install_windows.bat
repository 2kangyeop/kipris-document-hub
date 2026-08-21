@echo off
setlocal
title KIPRIS Downloader Setup
set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"

echo [1/4] Checking Python...
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo ERROR: Python 3.11 or newer is required.
echo Download: https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH" during installation.
pause
exit /b 1

:use_py
set "PY_CMD=py -3"
goto create_venv

:use_python
set "PY_CMD=python"

:create_venv
echo [2/4] Creating a private Python environment...
if not exist "%VENV_PY%" %PY_CMD% -m venv "%APP_DIR%.venv"
if not exist "%VENV_PY%" goto setup_error

echo [3/4] Checking required Python packages...
"%VENV_PY%" "%APP_DIR%verify_environment.py" >nul 2>nul
if not errorlevel 1 goto packages_ready

echo Installing missing or changed packages...
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
"%VENV_PY%" -m pip install --only-binary=:all: --no-warn-script-location -r "%APP_DIR%requirements.txt"
if errorlevel 1 goto setup_error
"%VENV_PY%" -m pip check
if errorlevel 1 goto setup_error
"%VENV_PY%" "%APP_DIR%verify_environment.py"
if errorlevel 1 goto setup_error
goto browser_setup

:packages_ready
echo Required packages are already installed. Download step skipped.

:browser_setup
echo [4/4] Browser setup...
echo Installed Google Chrome will be used first. Microsoft Edge is the fallback.
echo No Chromium download is needed.

echo.
echo Setup completed.
echo Run preview_windows.bat first to verify the app with visible diagnostics.
echo Double-click KIPRIS_Document_Hub.vbs to open the local web app without a command window.
pause
exit /b 0

:setup_error
echo.
echo ERROR: Setup did not complete.
echo If this is a company PC, check proxy/firewall settings or ask your administrator.
pause
exit /b 1
