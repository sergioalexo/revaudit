@echo off
setlocal
cd /d "%~dp0"
title RevAudit - start with Windows

rem Turns the "start RevAudit when I log in" entry on or off. No admin needed:
rem it's just a shortcut in the current user's Startup folder.
rem
rem   autostart.bat          ask (or show status and offer to change it)
rem   autostart.bat on       enable
rem   autostart.bat off      disable
rem   autostart.bat status   print current state, exit code 0 = on, 1 = off
rem
rem The logon shortcut only fires when you actually log on. A locked-but-
rem still-signed-in session that lost RevAudit (window closed, crash) stays
rem without it until the next logon. The keep-alive task covers that: every
rem 10 minutes it runs launch.bat --quiet, which does nothing at all when the
rem server is already answering on its port.
rem
rem   autostart.bat keepalive on / off / status

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set LNK=%STARTUP%\RevAudit.lnk

rem Under Scoop this file lives in a versioned folder (apps\revaudit\1.0.0\) that
rem goes away on `scoop update`; point the shortcut at the stable `current` junction
rem next to it instead. Plain git-clone installs just use this folder.
set APPDIR=%~dp0
if exist "%~dp0..\current\launch.bat" for %%I in ("%~dp0..\current") do set APPDIR=%%~fI\
set MODE=%~1
set TASK=RevAudit keep-alive
if /i "%MODE%"=="keepalive" (
  set MODE=keepalive
  if /i "%~2"=="on"     goto :ka_enable
  if /i "%~2"=="off"    goto :ka_disable
  if /i "%~2"=="status" goto :ka_status
  goto :ka_interactive
)
if /i "%MODE%"=="on"      goto :enable
if /i "%MODE%"=="enable"  goto :enable
if /i "%MODE%"=="off"     goto :disable
if /i "%MODE%"=="disable" goto :disable
if /i "%MODE%"=="status"  goto :status

rem ---- interactive ----
echo.
if exist "%LNK%" (
  echo   RevAudit currently STARTS with Windows ^(when you log in^).
  echo.
  choice /c YN /n /m "   Turn it off? [Y/N] "
  if errorlevel 2 goto :done
  goto :disable
) else (
  echo   RevAudit currently does NOT start with Windows.
  echo.
  choice /c YN /n /m "   Start it automatically when you log in? [Y/N] "
  if errorlevel 2 goto :done
  goto :enable
)

:enable
powershell -NoProfile -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$s=$w.CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%APPDIR%launch.bat';$s.WorkingDirectory='%APPDIR%';" ^
  "$s.WindowStyle=7;$s.IconLocation='shell32.dll,13';$s.Description='Start RevAudit at logon';$s.Save()" >nul 2>nul
if exist "%LNK%" (
  echo   [OK] Auto-start ON  -  RevAudit will start ^(minimised^) when you log in.
  set RC=0
) else (
  echo   [X]  Could not create the Startup shortcut.
  set RC=1
)
goto :done

:disable
if exist "%LNK%" del /q "%LNK%"
if exist "%LNK%" (
  echo   [X]  Could not remove the Startup shortcut.
  set RC=1
) else (
  echo   [OK] Auto-start OFF  -  use the Desktop shortcut to start RevAudit.
  set RC=0
)
goto :done

:status
if exist "%LNK%" (
  echo   Auto-start: ON   ^(%LNK%^)
  exit /b 0
) else (
  echo   Auto-start: OFF
  exit /b 1
)

rem ---- keep-alive scheduled task ----
:ka_interactive
echo.
schtasks /query /tn "%TASK%" >nul 2>nul
if not errorlevel 1 (
  echo   Keep-alive is ON: every 10 minutes RevAudit is restarted if it isn't running.
  echo.
  choice /c YN /n /m "   Turn it off? [Y/N] "
  if errorlevel 2 goto :done
  goto :ka_disable
) else (
  echo   Keep-alive is OFF: if RevAudit stops, it stays stopped until the next logon.
  echo.
  choice /c YN /n /m "   Restart it automatically every 10 minutes if it's down? [Y/N] "
  if errorlevel 2 goto :done
  goto :ka_enable
)

:ka_enable
rem wscript runs keepalive.vbs with no console of its own; the .vbs starts
rem launch.bat minimised, and launch.bat --quiet exits at once if the port
rem is already answering. Runs only while this user is signed in - which is
rem exactly when the server can hold the mapped drives it needs.
schtasks /create /f /tn "%TASK%" /sc minute /mo 10 ^
  /tr "wscript.exe \"%APPDIR%keepalive.vbs\"" >nul 2>nul
schtasks /query /tn "%TASK%" >nul 2>nul
if not errorlevel 1 (
  echo   [OK] Keep-alive ON  -  RevAudit is restarted within 10 minutes if it stops.
  set RC=0
) else (
  echo   [X]  Could not create the scheduled task.
  set RC=1
)
goto :done

:ka_disable
schtasks /delete /f /tn "%TASK%" >nul 2>nul
schtasks /query /tn "%TASK%" >nul 2>nul
if errorlevel 1 (
  echo   [OK] Keep-alive OFF.
  set RC=0
) else (
  echo   [X]  Could not remove the scheduled task.
  set RC=1
)
goto :done

:ka_status
schtasks /query /tn "%TASK%" >nul 2>nul
if not errorlevel 1 (
  echo   Keep-alive: ON   ^(task "%TASK%", every 10 minutes^)
  exit /b 0
) else (
  echo   Keep-alive: OFF
  exit /b 1
)

:done
if not defined RC set RC=0
if "%MODE%"=="" (
  echo.
  pause
)
if /i "%MODE%"=="keepalive" if "%~2"=="" (
  echo.
  pause
)
exit /b %RC%
