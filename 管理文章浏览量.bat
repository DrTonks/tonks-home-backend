@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 manage_article_views.py
) else (
    python manage_article_views.py
)

echo.
pause
endlocal
