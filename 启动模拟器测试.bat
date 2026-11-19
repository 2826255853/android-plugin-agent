@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch-android-emulator.ps1" %*
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
