@echo off
REM ---------------------------------------------------------------
REM  Futures Research Workbench - one-click launcher
REM  Requires: Python 3.10+   (no third-party pip packages needed)
REM ---------------------------------------------------------------
cd /d "%~dp0"

echo ==========================================================
echo   Futures Technical + Fundamental Analysis Workbench
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Please install Python 3.10+ from https://www.python.org/downloads/
    echo         Remember to tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] Python version too old. 3.10+ is required.
    python --version
    echo.
    pause
    exit /b 1
)

echo [OK] Python check passed.
echo [INFO] Starting server, browser will open automatically.
echo [INFO] Keep this window open while using the workbench.
echo [INFO] Press Ctrl+C or close this window to stop the server.
echo.

set OPEN_BROWSER=1
python app\server.py

echo.
echo Server stopped.
pause
