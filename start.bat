@echo off
setlocal enabledelayedexpansion
:: start.bat - Windows launcher for Embot
:: Automatically initializes uv project structure and manages dependencies.

cd /d "%~dp0"
set "SCRIPT_DIR=%~dp0"
set "PATH=%USERPROFILE%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%"

:: Load .env if present
if exist "%SCRIPT_DIR%.env" (
    for /f "usebackq tokens=1,2 delims==" %%a in ("%SCRIPT_DIR%.env") do (
        set "%%a=%%b"
    )
)

:: Ensure uv is installed
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [start.bat] uv not found - installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%"
)

:: Project Initialization
if not exist "%SCRIPT_DIR%pyproject.toml" (
    echo [start.bat] Initializing new uv project...
    cd /d "%SCRIPT_DIR%"
    uv init --python 3.11 --no-workspace

    if exist "%SCRIPT_DIR%requirements.txt" (
        echo [start.bat] Migrating dependencies from requirements.txt...
        uv add -r requirements.txt
    )
)

:: Sync Environment
echo [start.bat] Syncing dependencies...
cd /d "%SCRIPT_DIR%"
uv sync --frozen --python 3.11

:: Restart loop
echo [start.bat] Starting Embot (press Ctrl+C to stop)...

:restart
uv run python "%SCRIPT_DIR%Embot.py" -dev
if %errorlevel% equ 42 (
    echo [start.bat] Auto-update completed, restarting immediately...
    goto restart
)
echo.
echo [start.bat] Embot exited (code %errorlevel%). Restarting in 3s (Ctrl+C to stop)...
timeout /t 3 /nobreak
if %errorlevel% equ 0 goto restart
