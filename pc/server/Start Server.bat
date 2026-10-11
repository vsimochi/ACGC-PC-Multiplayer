@echo off
rem Start Server.bat - starts the Animal Crossing multiplayer dedicated server with the settings in server_config.ini (see SERVER_SETUP.txt).
rem Keep this file, server_config.ini and server_starter.ps1 in the game folder, next to ACLauncher.exe.
setlocal
set "RC=2"
cd /d "%~dp0"
title Animal Crossing server starter

where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo PowerShell was not found. It is part of Windows 10 and 11; this starter needs it to check server_config.ini.
    goto :end
)
if not exist "%~dp0server_starter.ps1" (
    echo server_starter.ps1 was not found next to Start Server.bat. Keep the three starter files together.
    set "RC=2"
    goto :end
)

rem -ExecutionPolicy Bypass applies to this one run only; the Windows setting is not changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_starter.ps1" -Root "%~dp0." %*
set "RC=%errorlevel%"

:end
echo.
if not "%RC%"=="0" echo The starter finished with code %RC% - see the message above.
echo Press any key to close this window. (The server, if it started, keeps running in its own window.)
pause >nul
endlocal & exit /b %RC%
