@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "EDITOR_DIR=%~dp0"
set "EDITOR_PORT=4179"
for %%I in ("%EDITOR_DIR%..\..") do set "PROJECT_DIR=%%~fI"

if not exist "%EDITOR_DIR%app\server.mjs" goto :missing_server
if not exist "%PROJECT_DIR%\references\optionlist.md" goto :missing_optionlist
if not exist "%PROJECT_DIR%\references\optionlib.md" goto :missing_optionlib
if not exist "%PROJECT_DIR%\assets\payoff" goto :missing_payoff_dir

set "NODE_EXE="
for /f "delims=" %%I in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%I"
if not defined NODE_EXE goto :node_missing

set "NODE_TAG="
for /f "tokens=1 delims=." %%I in ('"%NODE_EXE%" --version 2^>nul') do set "NODE_TAG=%%I"
set "NODE_MAJOR=%NODE_TAG:v=%"
if not defined NODE_MAJOR goto :node_unreadable
if %NODE_MAJOR% LSS 18 goto :node_too_old

netstat -ano | findstr /R /C:":%EDITOR_PORT% .*LISTENING" >nul
if not errorlevel 1 goto :port_busy

start "Payoff SVG Editor Server" /D "%EDITOR_DIR%" "%ComSpec%" /k ""%NODE_EXE%" "%EDITOR_DIR%app\server.mjs""

set /a START_ATTEMPTS=0
:wait_for_server
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:%EDITOR_PORT%/api/products; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto :ready
set /a START_ATTEMPTS+=1
if %START_ATTEMPTS% GEQ 30 goto :server_timeout
timeout /t 1 /nobreak >nul
goto :wait_for_server

:ready
start "" "http://127.0.0.1:%EDITOR_PORT%"
endlocal
exit /b 0

:missing_server
echo.
echo [Payoff Editor] Start failed: app\server.mjs is missing.
goto :stop

:missing_optionlist
echo.
echo [Payoff Editor] Start failed: references\optionlist.md is missing.
echo Please download and extract the complete OptionHelper folder.
goto :stop

:missing_optionlib
echo.
echo [Payoff Editor] Start failed: references\optionlib.md is missing.
echo Please download and extract the complete OptionHelper folder.
goto :stop

:missing_payoff_dir
echo.
echo [Payoff Editor] Start failed: assets\payoff is missing.
echo Please download and extract the complete OptionHelper folder.
goto :stop

:node_missing
echo.
echo [Payoff Editor] Start failed: Node.js 18 or later was not found.
echo Install Node.js 18+ and run this starter again.
goto :stop

:node_unreadable
echo.
echo [Payoff Editor] Start failed: Node.js version could not be read.
echo Install Node.js 18+ and run this starter again.
goto :stop

:node_too_old
echo.
echo [Payoff Editor] Start failed: Node.js %NODE_TAG% is below version 18.
echo Upgrade Node.js to version 18 or later and run this starter again.
goto :stop

:port_busy
echo.
echo [Payoff Editor] Start failed: port %EDITOR_PORT% is already in use.
echo Close the existing Payoff Editor server, then run this starter again.
goto :stop

:server_timeout
echo.
echo [Payoff Editor] Start failed: the server did not respond within 30 seconds.
echo Check the Payoff SVG Editor Server window for the detailed Node.js error.
goto :stop

:stop
echo.
pause
endlocal
exit /b 1
