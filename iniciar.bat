@echo off
title Lorota Converter
cd /d "%~dp0"
if not exist .venv python -m venv .venv
rem Instala/atualiza dependencias (o yt-dlp muda com frequencia)
.venv\Scripts\python.exe -m pip install -q -U flask yt-dlp pywebview
rem Abre a janela do app. Para usar no navegador: iniciar.bat --browser
start "" .venv\Scripts\pythonw.exe app.py %*
