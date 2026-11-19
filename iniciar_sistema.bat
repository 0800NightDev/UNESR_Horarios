@echo off
CHCP 65001 >nul
TITLE UNESR - Oferta Academica y Horarios
cd /d "%~dp0"

if exist "%~dp0dist\UNESR_Horarios.exe" (
  start "" "%~dp0dist\UNESR_Horarios.exe"
  exit /b 0
)

if not exist ".venv" (
  echo Creando entorno virtual...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt --quiet
python main.py
pause
