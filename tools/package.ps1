# Monta a pasta final (exe + ffmpeg + leia-me) e compacta em "Lorota Converter.zip"
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$rel = Join-Path $root 'release\Lorota Converter'
New-Item -ItemType Directory -Force $rel | Out-Null

Copy-Item (Join-Path $root 'dist\Lorota Converter.exe') $rel -Force
$ff = Split-Path (Get-Command ffmpeg).Source
Copy-Item (Join-Path $ff 'ffmpeg.exe'), (Join-Path $ff 'ffprobe.exe') $rel -Force

@"
LOROTA CONVERTER
================

1. Extraia esta pasta inteira em qualquer lugar (ex.: Area de Trabalho).
2. De dois cliques em "Lorota Converter.exe".
   - Se o Windows mostrar "O Windows protegeu o computador",
     clique em "Mais informacoes" > "Executar assim mesmo".
3. A janela do Lorota abre. Cole o link e converta.
4. Para desligar, e so fechar a janela.

Os arquivos ficam salvos na pasta "downloads", ao lado do .exe
(o botao "Abrir pasta" leva direto ate eles).
Mantenha ffmpeg.exe e ffprobe.exe na mesma pasta do .exe.
Nao precisa instalar Python nem nada.
"@ | Set-Content -Encoding utf8 (Join-Path $rel 'LEIA-ME.txt')

$zip = Join-Path $root 'Lorota Converter.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $rel -DestinationPath $zip -CompressionLevel Optimal
"Pronto: $zip ({0:N0} MB)" -f ((Get-Item $zip).Length / 1MB)
