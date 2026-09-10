@echo off
setlocal
cd /d "%~dp0"
title RevAudit - close this window to stop the server

rem --- pick a python ---
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
  echo Python was not found. Install Python 3.9+ from https://www.python.org/downloads/
  echo and tick "Add Python to PATH", then run this again ^(or run install.bat^).
  pause
  exit /b 1
)

rem --- first-run safety net: make the config and .env if install.bat wasn't used ---
if not exist "revaudit.conf" if exist "revaudit.conf.example" copy /y "revaudit.conf.example" "revaudit.conf" >nul
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul

if not exist ".env" (
  echo Missing .env in this folder. Run install.bat, or see README.md.
  pause
  exit /b 1
)

rem --- read the port out of revaudit.conf so the "already running" check matches ---
set PORT=8000
for /f "usebackq tokens=1,2 delims== " %%A in ("revaudit.conf") do (
  if /i "%%A"=="port" set PORT=%%B
)

rem --- if something is already listening there, just open the browser ---
powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient).Connect('localhost',%PORT%); exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo RevAudit is already running - opening it in your browser.
  start "" "http://localhost:%PORT%"
  exit /b 0
)

rem --- auto-update: pull the latest code if GitHub is ahead ------------------
rem   Skipped when REVAUDIT_NO_UPDATE is set, when git isn't installed, or when
rem   this folder isn't a git clone. A fast-forward only - never touches local
rem   edits, and if it can't reach GitHub it just moves on and starts the server.
if defined REVAUDIT_NO_UPDATE goto :after_update
if not exist ".git" goto :after_update
where git >nul 2>nul || goto :after_update

echo Checking for updates ...
git fetch --quiet 2>nul
if errorlevel 1 (
  echo   offline or GitHub unreachable - starting the version you have.
  goto :after_update
)
for /f %%N in ('git rev-list --count HEAD..@{u} 2^>nul') do set BEHIND=%%N
if not defined BEHIND goto :after_update
if "%BEHIND%"=="0" (
  echo   already up to date.
  goto :after_update
)
echo   %BEHIND% new commit^(s^) on GitHub - updating ...
git pull --ff-only --quiet
if errorlevel 1 (
  echo   couldn't fast-forward ^(local changes?^) - starting the version you have.
) else (
  echo   updated. Restarting with the new code.
  start "" "%~f0"
  exit /b 0
)
:after_update

rem --- open the browser a couple seconds after the server comes up ---
start "" /min "%~dp0_open_browser.bat" %PORT%

echo Starting RevAudit ...
echo.
echo Closing this window stops the server for everyone using it.
echo.

rem -u keeps the address banner and request log from getting stuck in a buffer
%PY% -u serve.py

echo.
echo RevAudit stopped.
pause
