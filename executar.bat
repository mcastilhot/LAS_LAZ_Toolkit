@echo off
rem ==========================================================================
rem  Inicia o LAS / LAZ Toolkit
rem ==========================================================================
setlocal
cd /d "%~dp0"

if exist "%~dp0dist\Ferramentas_LAZ\Ferramentas_LAZ.exe" (
    cd /d "%~dp0dist\Ferramentas_LAZ"
    start "" "Ferramentas_LAZ.exe"
) else if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0Ferramentas_LAZ.py"
) else (
    start "" pythonw "%~dp0Ferramentas_LAZ.py"
)
