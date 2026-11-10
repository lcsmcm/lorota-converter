# Cria o atalho "Lorota Converter" na Area de Trabalho, abrindo o app pelo pythonw.exe do .venv
$proj = Split-Path $PSScriptRoot
$desk = [Environment]::GetFolderPath('Desktop')
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk 'Lorota Converter.lnk'))
$lnk.TargetPath = Join-Path $proj '.venv\Scripts\pythonw.exe'
$lnk.Arguments = '"' + (Join-Path $proj 'app.py') + '"'
$lnk.WorkingDirectory = $proj
$lnk.IconLocation = (Join-Path $proj 'docs\icon.ico') + ',0'
$lnk.Description = 'Lorota Converter'
$lnk.Save()
"Atalho criado em: $(Join-Path $desk 'Lorota Converter.lnk')"
