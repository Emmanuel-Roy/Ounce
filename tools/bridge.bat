@echo off
REM ===========================================================================
REM Ounce bridge launcher.
REM
REM Works standalone (double-click) and as a Steam shortcut. Launched from
REM Steam with Steam Input enabled, the controller is presented to this process
REM as an ordinary virtual gamepad, so it shows up in the list like any other
REM pad - which is the only way a Steam Controller is usable at all, since
REM outside Steam it is a keyboard/mouse device with no gamepad to detect.
REM
REM   Steam -> Games -> Add a Non-Steam Game -> Browse -> this file
REM   Right-click it -> Properties -> Controller -> Enable Steam Input
REM   Properties -> Controller -> Edit Layout   (bind sticks to Joystick Move,
REM                                              NOT D-Pad, or you lose analog)
REM
REM Run with no arguments and it asks which input drives each of the four
REM virtual controllers. Any arguments are passed through to test_bridge.py.
REM ===========================================================================

setlocal

REM SDL's direct Steam Controller HID driver would claim the pad before Steam
REM Input sees it, bypassing the layout you configured. test_bridge.py only
REM sets this if it is not already defined, so setting it here wins.
set SDL_JOYSTICK_HIDAPI_STEAM=0

cd /d "%~dp0"

python test_bridge.py %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo [Ounce] Exited with code %EXITCODE%.
  echo [Ounce] If python was not found, install it or use a full path above.
)

REM Steam leaves no readable console after exit, so hold the window open.
REM Set OUNCE_NOPAUSE=1 to skip this when scripting.
if not "%OUNCE_NOPAUSE%"=="1" pause
endlocal
