@echo off
setlocal
title KIPRIS Document Hub - Preview and Diagnostics
set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"

echo ============================================================
echo  KIPRIS Document Hub 6.13 - pre-release preview
echo ============================================================
echo.

if not exist "%VENV_PY%" (
  echo [ERROR] The private Python environment was not found.
  echo Run install_windows.bat first.
  echo.
  pause
  exit /b 1
)

echo [1/3] Checking installed package versions...
"%VENV_PY%" "%APP_DIR%verify_environment.py"
if errorlevel 1 goto preview_error

echo [2/3] Checking program files and imports...
"%VENV_PY%" "%APP_DIR%launch_web.py" --check
if errorlevel 1 goto preview_error

echo [3/3] Starting the local web app in diagnostic mode...
echo A Chrome window should open automatically.
echo Keep this window open while testing. Server errors will appear here.
echo Use the top-right Exit button in the web app to finish the preview.
echo.
"%VENV_PY%" "%APP_DIR%launch_web.py" --debug
set "PREVIEW_RESULT=%ERRORLEVEL%"

echo.
if not "%PREVIEW_RESULT%"=="0" (
  echo [ERROR] Preview exited with code %PREVIEW_RESULT%.
  echo Startup log: %LOCALAPPDATA%\KIPRISDocumentHub\startup.log
) else (
  echo Preview finished normally.
)
pause
exit /b %PREVIEW_RESULT%

:preview_error
echo.
echo [ERROR] The preview check failed.
echo Run install_windows.bat again, then retry preview_windows.bat.
echo Startup log: %LOCALAPPDATA%\KIPRISDocumentHub\startup.log
pause
exit /b 1
