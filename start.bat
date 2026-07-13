@echo off
title PDFEdit Launcher
echo Starting PDFEdit Application...

:: 1. Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.10 or newer and try again.
    pause
    exit /b
)

:: 2. Check if Node.js is installed
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js is not installed or not in your system PATH.
    echo Please install Node.js 18 or newer and try again.
    pause
    exit /b
)

:: 3. Check for node_modules, if missing run npm install
if not exist "node_modules" (
    echo [INFO] npm dependencies not found. Installing...
    call npm install
)

:: 4. Check if python dependencies are installed
echo Checking Python dependencies...
python -c "import fastapi, uvicorn, fitz, PIL, numpy, dotenv, replicate, markdown, playwright, magic_pdf" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Some Python dependencies are missing. Installing requirements...
    pip install -r requirements.txt
    pip install -U "mineru[core]"
    echo Installing Playwright Chromium browser...
    python -m playwright install chromium
) else (
    echo Python dependencies are already satisfied.
)

:: 5. Start the Python backend FastAPI server
echo Starting Python backend server...
start cmd /k "python -m uvicorn main:app --host 127.0.0.1 --port 8007"

:: 6. Start the Vite frontend dev server
echo Starting React/Vite frontend server...
start cmd /k "npm run dev"

echo.
echo PDFEdit servers are launching!
echo Backend API: http://127.0.0.1:8007/docs
echo Frontend URL: http://localhost:5173/
echo.
pause
