@echo off
setlocal
echo ===================================================
echo   DocForensics Analyzer - Standalone Build Script
echo ===================================================
python build_exe.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Packaging encountered an error.
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo Packaging completed successfully!
pause
