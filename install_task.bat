@echo off
:: Run this once as Administrator to register Embot as a startup task.
:: Replace YOURUSERNAME below with your actual Windows username (e.g. Embis).

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=Embot"
set "USERNAME=Embis"

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%SCRIPT_DIR%start.bat\"" ^
  /sc onstart ^
  /ru "%COMPUTERNAME%\%USERNAME%" ^
  /rl HIGHEST ^
  /it ^
  /f

echo.
echo [install_task.bat] Task "%TASK_NAME%" registered for user %USERNAME%.
echo Embot will start automatically on boot without requiring a login.
echo You will be prompted for your Windows password to complete registration.
echo.
echo To remove:  schtasks /delete /tn "%TASK_NAME%" /f
echo To run now: schtasks /run /tn "%TASK_NAME%"
