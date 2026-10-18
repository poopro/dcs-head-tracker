@echo off
rem Double-click me once: installs the requirements, then starts the app.
cd /d "%~dp0"
call "%~dp0_py.bat"
if not defined PY (
  echo Python was not found on this PC.
  echo Install Python 3.12 from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo and double-click INSTALL.bat again.
  pause
  exit /b 1
)
echo Using Python: %PY%
%PY% --version
echo.
echo Installing requirements ^(a few minutes the first time^)...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo INSTALL FAILED - please read the messages above ^(or send me a screenshot^).
  pause
  exit /b 1
)
echo.
echo Installed. Creating the desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
echo.
echo Starting DCS Head Tracker...
start "" wscript.exe "%~dp0launch.vbs"
echo.
echo Done. From now on just double-click "DCS Head Tracker" on the desktop,
echo or do nothing: it starts with Windows and begins tracking when DCS opens.
timeout /t 6 >nul
