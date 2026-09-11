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

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set LNK=%STARTUP%\RevAudit.lnk

rem Under Scoop this file lives in a versioned folder (apps\revaudit\1.0.0\) that
rem goes away on `scoop update`; point the shortcut at the stable `current` junction
rem next to it instead. Plain git-clone installs just use this folder.
set APPDIR=%~dp0
if exist "%~dp0..\current\launch.bat" for %%I in ("%~dp0..\current") do set APPDIR=%%~fI\
set MODE=%~1
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

:done
if not defined RC set RC=0
if "%MODE%"=="" (
  echo.
  pause
)
exit /b %RC%
