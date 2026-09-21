@echo off
setlocal
cd /d "%~dp0"
title RevAudit - close this window to stop the server

rem --quiet: started by the keep-alive task, not a person. Exit silently when
rem RevAudit is already up, and don't pop a browser when it has to start it.
set QUIET=
if /i "%~1"=="--quiet" set QUIET=1

rem One line per attempt, so "it didn't start this morning" has an answer.
rem revaudit.log (written by serve.py) has the server's own output. The
rem keep-alive task's every-10-minutes "still running" checks are not logged;
rem only the times it found RevAudit down and had to start it.
set LAUNCHLOG=%~dp0launch.log
if not defined QUIET >>"%LAUNCHLOG%" echo %date% %time%  launch.bat %*

rem --- pick a python ---
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
  >>"%LAUNCHLOG%" echo %date% %time%    python not found
  echo Python was not found. Install Python 3.9+ from https://www.python.org/downloads/
  echo and tick "Add Python to PATH", then run this again ^(or run install.bat^).
  pause
  exit /b 1
)

rem --- first-run safety net: make the config and .env if install.bat wasn't used ---
if not exist "revaudit.conf" if exist "revaudit.conf.example" copy /y "revaudit.conf.example" "revaudit.conf" >nul
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul

if not exist ".env" (
  >>"%LAUNCHLOG%" echo %date% %time%    no .env
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
  if defined QUIET exit /b 0
  >>"%LAUNCHLOG%" echo %date% %time%    already running on port %PORT% - opening browser
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
  >>"%LAUNCHLOG%" echo %date% %time%    pulled %BEHIND% commit(s), relaunching
  start "" "%~f0" %*
  exit /b 0
)
:after_update

rem --- open the browser a couple seconds after the server comes up ---
if not defined QUIET start "" /min "%~dp0_open_browser.bat" %PORT%

if defined QUIET >>"%LAUNCHLOG%" echo %date% %time%  keep-alive found RevAudit down
>>"%LAUNCHLOG%" echo %date% %time%    starting serve.py on port %PORT%
echo Starting RevAudit ...
echo.
echo Closing this window stops the server for everyone using it.
echo.

rem -u keeps the address banner and request log from getting stuck in a buffer
%PY% -u serve.py
set RC=%errorlevel%
>>"%LAUNCHLOG%" echo %date% %time%    serve.py exited with code %RC% - see revaudit.log

echo.
echo RevAudit stopped ^(exit code %RC%^). Details are in revaudit.log.
if not defined QUIET pause
exit /b %RC%
