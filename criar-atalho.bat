@echo off
rem Cria o atalho "Lorota Converter" na Area de Trabalho.
rem Ele abre o app pelo Python (que tem assinatura digital), entao funciona
rem mesmo com o Controle Inteligente de Aplicativos do Windows ligado.
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python.exe -m pip install -q -U flask yt-dlp pywebview
powershell -NoProfile -ExecutionPolicy Bypass -File tools\criar-atalho.ps1
pause
