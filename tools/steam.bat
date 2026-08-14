@echo off
REM ===========================================================================
REM Ounce bridge, launched under Steam Input.
REM
REM Steam only applies Steam Input to processes IT launches, so this file must
REM be added to Steam as a Non-Steam Game and started from there. Double-
REM clicking it works, but you get the raw controller with no Steam remapping,
REM which defeats the point.
REM
REM   1. Steam -> Games -> Add a Non-Steam Game to My Library -> Browse
REM        -> select this steam.bat
REM   2. Right-click it in your library -> Properties -> Controller
REM        -> "Enable Steam Input"
REM   3. Properties -> Controller -> Edit Layout  (configure the pad here)
REM   4. Launch it from Steam
REM
REM Anything in the shortcut's Launch Options is forwarded to test_bridge.py:
REM   --assign 0=pad:Steam --assign 1,2,3=keyboard
REM ===========================================================================

setlocal

REM Steam Input exposes its own virtual gamepad. SDL's direct Steam Controller
REM HID driver would claim the pad first and bypass Steam entirely, so the
REM layout you configure would do nothing. Force it off here.
REM (test_bridge.py only sets these if they are not already defined.)
set SDL_JOYSTICK_HIDAPI_STEAM=0

REM Let SDL see Steam's virtual gamepad rather than the raw device.
set SDL_JOYSTICK_RAWINPUT=0

cd /d "%~dp0"

set OUNCE_ARGS=%*
if "%OUNCE_ARGS%"=="" set OUNCE_ARGS=--assign 0=pad --assign 1,2,3=keyboard --relaunch-seconds 0

echo [Ounce] Steam Input mode (SDL_JOYSTICK_HIDAPI_STEAM=0)
echo [Ounce] Args: %OUNCE_ARGS%
echo.

python test_bridge.py %OUNCE_ARGS%
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
  echo [Ounce] Bridge exited with code %EXITCODE%.
  echo [Ounce] If python was not found, install it or put a full path above.
)

REM Steam gives no readable console after exit, so hold the window open rather
REM than letting an error flash past. Set OUNCE_NOPAUSE=1 to skip (for scripts).
if not "%OUNCE_NOPAUSE%"=="1" pause
endlocal
