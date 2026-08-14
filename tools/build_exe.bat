@echo off
REM ===========================================================================
REM Build bin\OunceBridge.exe - a standalone bridge with no Python needed.
REM
REM The exe exists mainly so Steam can see it: Steam's "Add a Non-Steam Game"
REM browser filters to *.exe, so a .bat is invisible there unless you switch
REM the file-type dropdown to All Files.
REM
REM Requires: pip install pyinstaller
REM ===========================================================================

setlocal
cd /d "%~dp0"

where python >nul 2>&1 || (echo [-] python not found in PATH & exit /b 1)
python -c "import PyInstaller" 2>nul || (
  echo [-] PyInstaller not installed.  Run:  pip install pyinstaller
  exit /b 1
)

REM --onedir, NOT --onefile. A onefile build unpacks itself and then relaunches
REM as a CHILD process, and Steam Input only hooks the process Steam actually
REM launched - so the pad would be read by a process Steam never instrumented,
REM and the controller stays stuck in desktop/mouse mode. onedir keeps it a
REM single process, which is what Steam needs.
echo [Ounce] Building OunceBridge.exe ...
python -m PyInstaller ^
  --onedir ^
  --console ^
  --name OunceBridge ^
  --distpath ..\bin ^
  --workpath "%TEMP%\ounce_pyi_build" ^
  --specpath "%TEMP%\ounce_pyi_build" ^
  --hidden-import pygame ^
  --hidden-import serial.tools.list_ports ^
  test_bridge.py

if errorlevel 1 (
  echo.
  echo [-] Build failed.
  exit /b 1
)

echo.
echo [+] Built ..\bin\OunceBridge\OunceBridge.exe
echo     Add THAT exe to Steam as a Non-Steam Game, then enable Steam Input.
echo     Keep the whole OunceBridge folder together - the exe needs _internal\.
endlocal
