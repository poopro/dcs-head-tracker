@echo off
rem Command-line version. Pass options through, e.g.  run.bat --no-preview --invert-yaw
cd /d "%~dp0"
call "%~dp0_py.bat"
if not defined PY (
  echo Python not found. Run INSTALL.bat first.
  pause
  exit /b 1
)
%PY% dcs_headtracker.py %*
pause
