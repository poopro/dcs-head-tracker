@echo off
rem Builds dist\DCS-HeadTracker\DCS-HeadTracker.exe  (a folder; copy the whole folder anywhere).
rem The face model is downloaded once and bundled, so the exe works offline.
rem   build_exe.bat          -> windowed app (no console)
rem   build_exe.bat debug    -> keeps a console window so start-up errors are visible
cd /d "%~dp0"
call "%~dp0_py.bat"
if not defined PY (
  echo Python not found. Run INSTALL.bat first.
  pause
  exit /b 1
)
set MODE=--windowed
if /i "%1"=="debug" set MODE=--console

%PY% -m pip install -r requirements.txt pyinstaller || goto :fail
%PY% -c "import shutil; from dcs_headtracker import ensure_model; shutil.copy(ensure_model(None), 'face_landmarker.task')" || goto :fail
%PY% -m PyInstaller --noconfirm --onedir %MODE% --name DCS-HeadTracker ^
  --icon headtracker.ico --collect-all mediapipe ^
  --add-data "face_landmarker.task;." --add-data "headtracker.ico;." dcs_headtracker_gui.py || goto :fail

echo.
echo Done:  dist\DCS-HeadTracker\DCS-HeadTracker.exe
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
