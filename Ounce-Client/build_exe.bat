@echo off
REM ===========================================================================
REM Build bin\client\OunceClient.exe - a standalone bridge with no Python needed.
REM
REM The exe exists mainly so Steam can see it: Steam's "Add a Non-Steam Game"
REM browser filters to *.exe, so a .bat is invisible there unless you switch
REM the file-type dropdown to All Files.
REM
REM ounce.ico is the exe icon (what Steam and Explorer show) and carries the
REM wordmark on its 48px-and-up frames, the character alone below that;
REM ounce_icon.png is the character crop bundled for pygame.display.set_icon().
REM Both are generated from Graphics\icon.png - see Graphics\README.md beside
REM this script.
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
echo [Ounce] Building OunceClient.exe ...
REM --noconfirm: without it PyInstaller stops to ask before replacing an
REM existing bin\client\OunceClient, which fails outright when the build is not
REM run from an interactive console. It is why recordings and the keymap are
REM kept OUTSIDE that folder - see recordings_root() / keymap_store_path().
REM Both icon paths are absolute: --icon and --add-data resolve relative to
REM --specpath, and the spec lives under %TEMP%.
python -m PyInstaller ^
  --noconfirm ^
  --onedir ^
  --console ^
  --name OunceClient ^
  --distpath ..\bin\client ^
  --workpath "%TEMP%\ounce_pyi_build" ^
  --specpath "%TEMP%\ounce_pyi_build" ^
  --icon "%~dp0ounce.ico" ^
  --add-data "%~dp0ounce_icon.png;." ^
  --hidden-import pygame ^
  --hidden-import serial.tools.list_ports ^
  test_bridge.py

if errorlevel 1 (
  echo.
  echo [-] Build failed.
  exit /b 1
)

echo.
echo [+] Built ..\bin\client\OunceClient\OunceClient.exe
echo     Add THAT exe to Steam as a Non-Steam Game, then enable Steam Input.
echo     Keep the whole OunceClient folder together - the exe needs _internal\.
endlocal
