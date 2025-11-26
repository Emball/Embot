@echo off
:restart
"C:\Users\Embis\Documents\Embot\Winpython64-3.11.9.0dotb5\python-3.11.9.amd64\python.exe" -m pip install -r requirements.txt
"C:\Users\Embis\Documents\Embot\Winpython64-3.11.9.0dotb5\python-3.11.9.amd64\python.exe" main.py -dev -test
pause
goto :restart