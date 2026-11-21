@echo off
rem Remove the keep-alive (the Windows Task Scheduler entry named "FX-NEWS mihari"). The watcher itself keeps running.
call "%~dp0fx.bat" autostart-off
