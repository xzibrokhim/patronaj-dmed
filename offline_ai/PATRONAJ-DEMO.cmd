@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
set "PYTHONHOME="
set "INTERPRETER=python\python.exe"
if not exist "%INTERPRETER%" set "INTERPRETER=.venv\Scripts\python.exe"
if not exist "%INTERPRETER%" (
    echo Python muhiti topilmadi. Avval prepare.ps1 orqali loyiha muhitini tayyorlang.
    pause
    exit /b 1
)
"%INTERPRETER%" -X utf8 patronage_demo.py --open %*
if errorlevel 1 pause
