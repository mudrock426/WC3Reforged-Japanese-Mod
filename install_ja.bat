@echo off
cd /d "%~dp0"
echo ====================================================
echo  Warcraft III: Reforged Japanese Mod Installer
echo ====================================================
echo.
python -X utf8 patcher.py install
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found or an error occurred.
    echo Please install Python 3.10+ and try again.
)
echo.
pause
