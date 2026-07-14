@echo off
title PDFEdit Portable Launcher
echo Starting PDFEdit Application using Portable Python...

if not exist "python311\python.exe" (
    echo [ERROR] Portable python not found! Did you run setup_portable.ps1?
    pause
    exit /b
)

if not exist "dist\" (
    echo [ERROR] Frontend 'dist' folder not found! Make sure setup_portable.ps1 built the frontend.
    pause
    exit /b
)

echo Starting Python backend server...
start cmd /k ".\python311\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8007"

echo.
echo PDFEdit server is launching!
echo Open http://127.0.0.1:8007 in your browser to use the app!
echo.
pause
