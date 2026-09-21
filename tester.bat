@echo off
rem Affiche ce que le bot publierait, sans rien envoyer sur Discord.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Lance d'abord lancer.bat une premiere fois.
    pause
    exit /b 1
)

.venv\Scripts\python.exe bot.py --test
pause
