@echo off
rem Start watching in the background, with no console window left open. Stop it with stop.bat.
rem Safe to double-click again: if it is already running, this only shows the status.
setlocal
cd /d "%~dp0"
set "FXNEWS_NOPAUSE=1"
call "%~dp0fx.bat" start
echo.
echo  [FX-NEWS] This window closes by itself in 15 seconds.
ping -n 16 127.0.0.1 >nul
exit /b 0
