@echo off
setlocal
cd /d "%~dp0"

set "PORT=8000"

rem Find the conda environment without requiring "conda activate".
set "PYTHON="
if exist "%ProgramData%\miniconda3\envs\pdf-translator\python.exe" set "PYTHON=%ProgramData%\miniconda3\envs\pdf-translator\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\envs\pdf-translator\python.exe" set "PYTHON=%USERPROFILE%\miniconda3\envs\pdf-translator\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\envs\pdf-translator\python.exe" set "PYTHON=%USERPROFILE%\anaconda3\envs\pdf-translator\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\miniconda3\envs\pdf-translator\python.exe" set "PYTHON=%LOCALAPPDATA%\miniconda3\envs\pdf-translator\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\anaconda3\envs\pdf-translator\python.exe" set "PYTHON=%LOCALAPPDATA%\anaconda3\envs\pdf-translator\python.exe"

if not defined PYTHON (
    echo [ERROR] The conda environment "pdf-translator" was not found.
    echo.
    echo Create it first:
    echo     conda env create -f environment.yml
    echo.
    echo Or edit the PYTHON path in this run.bat.
    pause
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem If the port is already listening, open the running app instead of
rem starting a second server.
set "PORT_BUSY="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /C:":%PORT%" ^| findstr /C:"LISTENING"') do set "PORT_BUSY=1"

if defined PORT_BUSY (
    curl -s -m 2 "http://127.0.0.1:%PORT%/api/health" | findstr /C:"status" | findstr /C:"ok" >nul
    if not errorlevel 1 (
        echo Paper Translator is already running on port %PORT%.
        echo Opening http://127.0.0.1:%PORT%
        start "" "http://127.0.0.1:%PORT%"
        exit /b 0
    )
    echo [ERROR] Port %PORT% is already in use by another program.
    echo Close that program first, or change the PORT setting in run.bat.
    pause
    exit /b 1
)

echo Starting Paper Translator...
echo URL: http://127.0.0.1:%PORT%
echo.

"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
