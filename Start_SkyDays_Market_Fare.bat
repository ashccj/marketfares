@echo off
cd /d "%~dp0"
title SkyDays Market Fare Intelligence

echo.
echo ==========================================
echo   SkyDays Market Fare Intelligence
echo ==========================================
echo.
echo Checking required packages...
python -m pip install -r requirements.txt

echo.
echo Starting application...
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:8765"
python app.py

pause
