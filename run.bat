@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Please run the README install steps first:
    echo py -3.12 -m venv .venv
    echo .venv\Scripts\activate
    echo pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
