@echo off
rem Start watching in the background, with no console window left open. Stop it with stop.bat.
rem Safe to double-click again: a second copy notices the first one and exits by itself.
setlocal
cd /d "%~dp0"
set "FXNEWS_NOPAUSE=1"
rem Show the version first. (If config.ini sets console_rect, this also moves this window out of the way at once.)
call "%~dp0fx.bat" version
if errorlevel 1 exit /b 1
set "PYW="
where pythonw >nul 2>nul
if not errorlevel 1 set "PYW=pythonw"
if not defined PYW (
  where pyw >nul 2>nul
  if not errorlevel 1 set "PYW=pyw"
)
if defined PYW (
  start "" %PYW% run.py run
) else (
  echo  [FX-NEWS] pythonw was not found. Starting in a minimized window instead.
  start "FX-NEWS" /min cmd /c call "%~dp0fx.bat" run
)
rem Give it a few seconds to start, then show whether it is running.
ping -n 7 127.0.0.1 >nul
call "%~dp0fx.bat" status
echo.
echo  [FX-NEWS] This window closes by itself in 15 seconds.
ping -n 16 127.0.0.1 >nul
exit /b 0
