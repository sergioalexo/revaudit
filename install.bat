@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title RevAudit installer

echo.
echo   ================================================
echo     RevAudit  -  installer
echo   ================================================
echo.

rem ---------- are we running as administrator? ----------
net session >nul 2>nul
if errorlevel 1 ( set ADMIN=0 ) else ( set ADMIN=1 )

rem ---------- 1. find a Python ----------
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
  echo   [X] Python was not found.
  echo       Install Python 3.9 or newer from  https://www.python.org/downloads/
  echo       During setup, tick  "Add python.exe to PATH".  Then run this again.
  echo.
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo   [OK] Python !PYVER!   ^(!PY!^)

rem ---------- 2. config file ----------
if exist "revaudit.conf" (
  echo   [OK] revaudit.conf already exists  -  left as is
) else (
  copy /y "revaudit.conf.example" "revaudit.conf" >nul
  echo   [OK] revaudit.conf created from the template
  set OPENCONF=1
)

rem ---------- 3. secrets file ----------
if exist ".env" (
  echo   [OK] .env already exists
) else (
  copy /y ".env.example" ".env" >nul
  echo   [OK] .env created from the template  -  you still need to fill it in
  set NEEDENV=1
)

rem ---------- 4. Desktop shortcut ----------
powershell -NoProfile -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$s=$w.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\RevAudit.lnk');" ^
  "$s.TargetPath='%~dp0launch.bat';$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='shell32.dll,13';$s.Description='Start RevAudit';$s.Save()" >nul 2>nul
if exist "%USERPROFILE%\Desktop\RevAudit.lnk" (
  echo   [OK] Desktop shortcut:  RevAudit
) else (
  echo   [--] Could not create the Desktop shortcut  ^(not fatal^)
)

rem ---------- 5. start automatically when you log in (optional, no admin needed) ----------
rem   Re-running the installer keeps whatever you chose last time; change it any
rem   time with  autostart.bat  (on / off).
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
if exist "%STARTUP%\RevAudit.lnk" (
  echo   [OK] Auto-start:  already on  -  run autostart.bat to change it
) else (
  echo.
  choice /c YN /n /m "   Start RevAudit automatically when you log in to Windows? [Y/N] "
  if errorlevel 2 (
    echo   [--] Auto-start:  off  -  run autostart.bat later if you change your mind
  ) else (
    call "%~dp0autostart.bat" on
  )
)

rem ---------- 6. firewall rule (needs admin) ----------
if "!ADMIN!"=="1" (
  netsh advfirewall firewall show rule name="RevAudit (TCP 8000)" >nul 2>nul
  if errorlevel 1 (
    netsh advfirewall firewall add rule name="RevAudit (TCP 8000)" dir=in action=allow protocol=TCP localport=8000 profile=domain >nul
    echo   [OK] Firewall:  inbound TCP 8000 allowed on the Domain network
  ) else (
    echo   [OK] Firewall:  rule already present
  )
) else (
  echo   [--] Firewall rule needs administrator  -  see the note below
)

echo.
echo   ------------------------------------------------
if "!NEEDENV!"=="1" (
  echo    NEXT  -  put your Onshape API key into .env :
  echo        !PY! import_key.py           ^(if you saved the key to a file^)
  echo      or open .env in Notepad and paste it in by hand
  echo.
)
if "!ADMIN!"=="0" (
  echo    To open the firewall for other computers on the network:
  echo      right-click  install.bat  and choose  "Run as administrator",
  echo      then run it again.  It is safe to run twice.
  echo.
)
echo    Start RevAudit any time from the  RevAudit  shortcut on your Desktop.
echo    Turn "start with Windows" on or off with  autostart.bat
echo    Change the IP address or folders in  revaudit.conf  ^(plain text^).
echo   ------------------------------------------------
echo.

if "!OPENCONF!"=="1" (
  echo   Opening revaudit.conf so you can set the address and folders...
  timeout /t 2 >nul
  notepad "revaudit.conf"
)

pause
