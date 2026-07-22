@echo off
title Personal Status Service

cd /d "%~dp0"
if not exist "local.env.bat" (
    echo [ERROR] Missing local.env.bat
    echo Copy local.env.bat.example to local.env.bat and fill in your private values.
    pause
    exit /b 1
)
call "local.env.bat"

if "%SLEEPY_PYTHON%"=="" (
    echo [ERROR] SLEEPY_PYTHON is not configured in local.env.bat
    pause
    exit /b 1
)
if "%SLEEPY_SERVER_URL%"=="" (
    echo [ERROR] SLEEPY_SERVER_URL is not configured in local.env.bat
    pause
    exit /b 1
)
if "%SLEEPY_ADMIN_SECRET%"=="" (
    echo [ERROR] SLEEPY_ADMIN_SECRET is not configured in local.env.bat
    pause
    exit /b 1
)
if "%SLEEPY_STATUS_SECRET%"=="" (
    echo [ERROR] SLEEPY_STATUS_SECRET is not configured in local.env.bat
    pause
    exit /b 1
)
echo Current dir: %cd%
echo Python: %SLEEPY_PYTHON%
echo.

:: Step 1: Upload agent stats (once)
echo [%time%] Step 1/2: Uploading agent activity...
"%SLEEPY_PYTHON%" upload_agent_stats.py --server "%SLEEPY_SERVER_URL%" --secret "%SLEEPY_ADMIN_SECRET%"

if %errorlevel% neq 0 (
    echo.
    echo [WARN] Upload failed, code: %errorlevel%
    echo [WARN] Continuing to device tracker...
) else (
    echo [OK] Agent data uploaded
)
echo.
echo ========================================
echo.

:: Step 2: Device status tracker (foreground, keep-alive)
echo [%time%] Step 2/2: Starting device status reporter...
echo Press Ctrl+C to stop, or close this window
echo.
"%SLEEPY_PYTHON%" report_app.py

:: If report_app.py exits unexpectedly, pause
echo.
echo ========================================
echo [%time%] Script exited unexpectedly!
pause
