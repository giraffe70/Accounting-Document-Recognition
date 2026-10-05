@echo off
setlocal
cd /d "%~dp0"

rem Prefer the active Conda environment, then the standard installation.
set "PYEXE=%USERPROFILE%\Miniconda3\envs\pdfscan312\python.exe"
if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PYEXE=%CONDA_PREFIX%\python.exe"
set "HOST=127.0.0.1"
set "PORT=8090"

if not exist "%PYEXE%" (
    echo [ERROR] Python environment not found: "%PYEXE%"
    echo Activate the environment with: conda activate pdfscan312
    echo Install dependencies with:
    echo python -m pip install -r requirements.txt -r requirements-ocr.txt
    pause
    exit /b 1
)

"%PYEXE%" -c "import fastapi, uvicorn, sqlmodel, dotenv, multipart"
if errorlevel 1 (
    echo [ERROR] Required packages are missing. Run:
    echo python -m pip install -r requirements.txt -r requirements-ocr.txt
    pause
    exit /b 1
)

if /i "%~1"=="--check" (
    echo [OK] Launcher and core dependencies are ready.
    echo Python: "%PYEXE%"
    exit /b 0
)

echo ============================================
echo   Starting PDF Scan Recognition WebUI
echo   URL: http://%HOST%:%PORT%
echo ============================================
echo.

start "" "http://%HOST%:%PORT%"
"%PYEXE%" -m uvicorn app.main:app --host %HOST% --port %PORT%
set "SERVER_EXIT=%ERRORLEVEL%"
echo.
echo Server stopped. Exit code: %SERVER_EXIT%
pause
exit /b %SERVER_EXIT%
