@echo off
:: Run this once as Administrator to register Embot as a startup task.
:: Replace YOURUSERNAME below with your actual Windows username (e.g. Embis).

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=Embot"
set "USERNAME=Embis"

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%SCRIPT_DIR%start.bat\"" ^
  /sc onlogon /delay 0000:30 ^
  /ru "%COMPUTERNAME%\%USERNAME%" ^
  /rl HIGHEST ^
  /it ^
  /f

echo.
echo [install_task.bat] Task "%TASK_NAME%" registered for user %USERNAME%.
echo Embot will start 30s after %USERNAME% logs on. For unattended boots, enable Windows auto-login (netplwiz).
echo.
echo To remove:  schtasks /delete /tn "%TASK_NAME%" /f
echo To run now: schtasks /run /tn "%TASK_NAME%"
