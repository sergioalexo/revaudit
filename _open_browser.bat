@echo off
rem Helper for launch.bat - waits for the server to come up, then opens it.
rem Not meant to be run on its own. Arg 1 = port (default 8000).
set PORT=%~1
if "%PORT%"=="" set PORT=8000
timeout /t 2 >nul
start "" "http://localhost:%PORT%"
