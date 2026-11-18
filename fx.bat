@echo off
rem FX-NEWS launcher for Windows. Finds Python 3 and runs run.py with the given arguments.
setlocal
cd /d "%~dp0"
set "PY="
py --version >nul 2>nul
if not errorlevel 1 set "PY=py"
if not defined PY (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo.
  echo  [FX-NEWS] Python 3 was not found on this PC.
  echo  [FX-NEWS] Python 3 ga mitsukarimasen. Saki ni Python 3 wo irete kudasai.
  echo.
  pause
  exit /b 1
)
%PY% run.py %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
