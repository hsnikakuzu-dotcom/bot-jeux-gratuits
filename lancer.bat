@echo off
cd /d "%~dp0"

if not exist bot.py (
    echo Les fichiers du bot sont introuvables : extrais d'abord le zip ^(clic droit sur le zip, puis "Extraire tout"^).
    pause
    exit /b 1
)

rem Windows ajoute parfois ".txt" en cachette quand on renomme le fichier
if not exist .env if exist .env.txt ren .env.txt .env

if not exist .env (
    if not exist .env.example (
        echo Le fichier .env.example est introuvable : reextrais le zip en entier.
        pause
        exit /b 1
    )
    copy .env.example .env >nul
    echo Le fichier .env vient d'etre cree et s'ouvre dans le Bloc-notes.
    echo.
    echo  1. Colle le token du bot apres DISCORD_TOKEN=
    echo  2. Colle l'identifiant du salon apres GAMES_CHANNEL_ID=
    echo  3. Enregistre avec Ctrl+S et ferme le Bloc-notes
    echo  4. Relance lancer.bat
    echo.
    start "" notepad .env
    pause
    exit /b 0
)

if not exist .venv\Scripts\python.exe (
    echo Premiere installation, patiente un peu...
    python -m venv .venv || (echo Python est introuvable. Installe-le depuis python.org & pause & exit /b 1)
)

.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
.venv\Scripts\python.exe bot.py
pause
