@echo off
rem Switch to the newest version: stop the watcher, then start it again in the background.
setlocal
cd /d "%~dp0"
set "FXNEWS_NOPAUSE=1"
call "%~dp0fx.bat" stop
call "%~dp0fx.bat" start
echo.
echo  [FX-NEWS] This window closes by itself in 20 seconds.
ping -n 21 127.0.0.1 >nul
exit /b 0
