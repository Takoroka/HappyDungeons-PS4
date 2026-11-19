@echo off
rem Happy Dungeons (PS4, CUSA09120) in the portable shadPS4, against servers on this PC only.
rem Starts, if not running yet: the local shadNet (PSN stand-in, 127.0.0.1:31413), the local copy of the
rem Happy Dungeons server (127.0.0.1:5200) and the PS4 gateway in front of both (127.0.0.1:31313 and :5100),
rem then opens the game. Close the minimized server windows when you are done.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "EBOOT=D:\PS4 ROMS\CUSA09120\eboot.bin"

netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:31413 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting local shadNet...
    start "shadNet (local)" /min /d "%ROOT%shadnet" "%ROOT%shadnet\shadnet.exe"
)

netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:5200 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting the local Happy Dungeons server...
    start "Happy Dungeons server (local copy)" /min /d "%ROOT%livecopy\server" py -3 -u experimentalserver.py
)

netstat -ano -p TCP | findstr /r /c:"127\.0\.0\.1:5100 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting the PS4 gateway...
    start "Happy Dungeons PS4 gateway" /min /d "%ROOT%server" py -3 -I -u hd_ps4_gateway.py "%ROOT%livecopy\gateway.json"
)

rem Sound on for real play (hidden test runs set it to 0).
py -3 -I "%ROOT%tools\set_volume.py" 100
rem 30 fps: the game shows one frame per two vblanks.
py -3 -I "%ROOT%tools\set_cfg.py" "%ROOT%shadps4" GPU.vblank_frequency=60 >nul

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
