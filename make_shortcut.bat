@echo off
rem Re-creates the desktop shortcut (with icon).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
pause
