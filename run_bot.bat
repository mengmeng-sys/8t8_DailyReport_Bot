@echo off
cd /d "%~dp0"
:loop
echo [%date% %time%] starting bot >> bot.log
venv\Scripts\python.exe bot.py >> bot.log 2>&1
echo [%date% %time%] bot stopped, starting again in 15 seconds >> bot.log
ping 127.0.0.1 -n 16 >nul
goto loop
