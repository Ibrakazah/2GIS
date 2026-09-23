@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Zapusk servera... Ne zakryvai eto okno.
python app.py
pause
