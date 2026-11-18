@echo off
cd /d "%~dp0"
echo ====================================================
echo  Warcraft III: Reforged Japanese Mod Uninstaller
echo ====================================================
echo.
python -X utf8 patcher.py uninstall
echo.
pause
