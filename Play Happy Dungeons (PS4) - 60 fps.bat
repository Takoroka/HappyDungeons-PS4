@echo off
rem Happy Dungeons (PS4, CUSA09120) at 60 fps (pass 240 for 120 fps, 480 for 240 fps), against servers on this PC only.
rem Starts the local shadNet (PSN stand-in) and the Happy Dungeons services if they are not running yet,
rem then opens the game. Close the two minimized server windows when you are done.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "EBOOT=D:\PS4 ROMS\CUSA09120\eboot.bin"
set "VBLANK=120"
if not "%~1"=="" set "VBLANK=%~1"

netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:31313 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting local shadNet...
    start "shadNet (local)" /min /d "%ROOT%shadnet" "%ROOT%shadnet\shadnet.exe"
)

netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:5100 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting Happy Dungeons services...
    start "Happy Dungeons services (local)" /min /d "%ROOT%server" py -3 -I hd_services.py
)

rem Sound on for real play (hidden test runs set it to 0).
py -3 -I "%ROOT%tools\set_volume.py" 100
rem Frame rate = half the vblank rate (120 -> 60 fps, 240 -> 120 fps, 480 -> 240 fps); the HappyDungeonsFps patch keeps gameplay at normal speed.
py -3 -I "%ROOT%tools\set_cfg.py" "%ROOT%shadps4" GPU.vblank_frequency=%VBLANK% >nul
rem shadNet through 127.0.0.1:31313 (the gateway in front of shadNet): the emulator sends its STUN ping to the
rem shadNet port + 1 = 31314, where shadNet listens. Pointing it straight at 31413 breaks peer-to-peer play.
py -3 -I "%ROOT%tools\set_cfg.py" "%ROOT%shadps4" Network.shadnet_server=127.0.0.1:31313 >nul

for /l %%I in (1,1,10) do (
    netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:31313 .*LISTENING" >nul && goto :launch
    ping -n 2 127.0.0.1 >nul
)
echo shadNet did not start. Check the "shadNet (local)" window.
pause
exit /b 1

:launch
start "" /d "%ROOT%shadps4" "%ROOT%shadps4\shadPS4.exe" -g "%EBOOT%"
exit /b 0
