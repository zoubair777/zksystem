@echo off
rem Z.G ERP - rebuild dist\ERP\ERP.exe from the source code.
rem Safe for data: builds in a temp folder, then replaces ONLY ERP.exe and _internal.
rem dist\ERP\database, backups and logs are never touched.
rem (PyInstaller deletes its output folder, so never build directly into dist\ERP.)
setlocal
cd /d "%~dp0"

tasklist /FI "IMAGENAME eq ERP.exe" | find /I "ERP.exe" >nul
if not errorlevel 1 (
    echo [!] ERP.exe is running. Close the program first, then run this again.
    pause
    exit /b 1
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=py"
set "VENV=%LOCALAPPDATA%\ZG_ERP_build\venv"
set "OUT=%LOCALAPPDATA%\ZG_ERP_build\out"

if not exist "%VENV%\Scripts\python.exe" (
    echo Creating build environment...
    "%PY%" -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-desktop.txt pyinstaller || goto :fail

echo Building...
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name ERP ^
    --add-data "%CD%\templates;templates" --add-data "%CD%\static;static" ^
    --distpath "%OUT%\dist" --workpath "%OUT%\work" --specpath "%OUT%" "%CD%\desktop_launcher.py" || goto :fail

if not exist "dist\ERP" mkdir "dist\ERP"
echo Installing new build into dist\ERP (data folders untouched)...
copy /Y "%OUT%\dist\ERP\ERP.exe" "dist\ERP\ERP.exe" >nul || goto :fail
robocopy "%OUT%\dist\ERP\_internal" "dist\ERP\_internal" /MIR /NFL /NDL /NJH /NJS >nul
if errorlevel 8 goto :fail

echo.
echo Done. The app folder to copy to any computer is: %CD%\dist\ERP
pause
exit /b 0

:fail
echo.
echo [!] Build failed. Nothing in dist\ERP\database was changed.
pause
exit /b 1
