@echo off
rem Gera "Lorota Converter.zip" com o .exe + ffmpeg, pronto pra levar pra outro PC.
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python.exe -m pip install -q -U flask yt-dlp pywebview pyinstaller pillow
if not exist build mkdir build
.venv\Scripts\python.exe -I tools\make_icon.py build\icon.ico
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name "Lorota Converter" ^
  --icon build\icon.ico --add-data "static;static" --add-data "docs\icon.ico;docs" --collect-submodules yt_dlp app.py || exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\package.ps1
