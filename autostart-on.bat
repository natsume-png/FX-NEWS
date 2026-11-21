@echo off
rem Install the keep-alive: a Windows Task Scheduler entry named "FX-NEWS mihari" that starts the watcher
rem again within 5 minutes if it has stopped (for example after the PC was restarted).
rem It also stops the watcher and starts it again, so that the newest version is the one running.
rem Remove it with autostart-off.bat.
setlocal
cd /d "%~dp0"
set "FXNEWS_NOPAUSE=1"
call "%~dp0fx.bat" stop
call "%~dp0fx.bat" autostart-on
call "%~dp0fx.bat" start
echo.
echo  [FX-NEWS] This window closes by itself in 30 seconds.
ping -n 31 127.0.0.1 >nul
exit /b 0
