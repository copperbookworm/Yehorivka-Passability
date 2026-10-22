@echo off
cd /d "%~dp0"
if exist "%~dp0dist\map-water-bridge-annotator.exe" (
  start "" "%~dp0dist\map-water-bridge-annotator.exe"
) else (
  python "%~dp0tools\annotate_maps.py"
)
