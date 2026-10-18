@echo off
rem Opens the DCS Head Tracker window. It sets up and starts opentrack by itself.
rem   run_gui.bat          -> normal (console shows errors)
rem   run_gui.bat hidden   -> used by the desktop shortcut: no console, errors go to error.log
cd /d "%~dp0"
call "%~dp0_py.bat"
if not defined PY (
  if /i "%1"=="hidden" goto :hidden_fail
  echo Python not found. Run INSTALL.bat first.
  pause
  exit /b 1
)
if /i "%1"=="hidden" goto :hidden
%PY% dcs_headtracker_gui.py
if errorlevel 1 (
  echo.
  echo The program exited with an error. Did you run INSTALL.bat ?
  pause
)
exit /b 0

:hidden
%PY% dcs_headtracker_gui.py 2> "%~dp0error.log"
if errorlevel 1 goto :hidden_fail
exit /b 0

:hidden_fail
powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show('DCS Head Tracker could not start. Double-click INSTALL.bat first (details: error.log in the program folder).','DCS Head Tracker')"
exit /b 1
