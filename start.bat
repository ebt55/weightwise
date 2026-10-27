@echo off
setlocal
cd /d "%~dp0"
title WeightWise - Duplicate Weight Scanner
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 app.py %*
) else (
    python app.py %*
)
if errorlevel 1 (
    echo.
    echo WeightWise could not start. Review the message above.
    pause
)
endlocal
