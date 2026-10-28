@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw "%~dp0app.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python "%~dp0app.py"
    pause
    exit /b 0
)

echo Python 3 was not found. Please install Python 3.10+ first.
pause
