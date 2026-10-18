@echo off
rem Finds a working Python and puts it in the PY variable (used by the other .bat files).
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if defined PY goto :eof
python -c "import sys" >nul 2>nul && set "PY=python"
if defined PY goto :eof
if exist "%USERPROFILE%\anaconda3\python.exe" set PY="%USERPROFILE%\anaconda3\python.exe"
if defined PY goto :eof
if exist "%USERPROFILE%\miniconda3\python.exe" set PY="%USERPROFILE%\miniconda3\python.exe"
if defined PY goto :eof
if exist "C:\ProgramData\anaconda3\python.exe" set PY="C:\ProgramData\anaconda3\python.exe"
