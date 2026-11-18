@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
set "PYTHONHOME="
set "INTERPRETER=python\python.exe"
if not exist "%INTERPRETER%" set "INTERPRETER=.venv\Scripts\python.exe"
"%INTERPRETER%" -X utf8 voice_visit.py %*
pause
