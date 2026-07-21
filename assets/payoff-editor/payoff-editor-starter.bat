@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "EDITOR_DIR=%~dp0"
set "EDITOR_PORT=4179"
for %%I in ("%EDITOR_DIR%..\..") do set "PROJECT_DIR=%%~fI"

if not exist "%EDITOR_DIR%app\server.mjs" (
  set "FAIL_MESSAGE=缺少app\server.mjs。请保留完整OptionHelper项目。"
  goto :fail
)
if not exist "%PROJECT_DIR%\references\optionlist.md" (
  set "FAIL_MESSAGE=缺少references\optionlist.md。请保留完整OptionHelper项目。"
  goto :fail
)
if not exist "%PROJECT_DIR%\references\optionlib.md" (
  set "FAIL_MESSAGE=缺少references\optionlib.md。请保留完整OptionHelper项目。"
  goto :fail
)
if not exist "%PROJECT_DIR%\assets\payoff" (
  set "FAIL_MESSAGE=缺少assets\payoff目录。请保留完整OptionHelper项目。"
  goto :fail
)

set "NODE_EXE="
for /f "delims=" %%I in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%I"
if not defined NODE_EXE (
  set "FAIL_MESSAGE=未找到Node.js。请安装Node.js 18或更高版本后重试。"
  goto :fail
)

set "NODE_TAG="
for /f "tokens=1 delims=." %%I in ('"%NODE_EXE%" --version 2^>nul') do set "NODE_TAG=%%I"
set "NODE_MAJOR=%NODE_TAG:v=%"
if not defined NODE_MAJOR (
  set "FAIL_MESSAGE=无法识别Node.js版本。请安装Node.js 18或更高版本后重试。"
  goto :fail
)
if %NODE_MAJOR% LSS 18 (
  set "FAIL_MESSAGE=当前Node.js版本为%NODE_TAG%。请升级至18或更高版本后重试。"
  goto :fail
)

netstat -ano | findstr /R /C:":%EDITOR_PORT% .*LISTENING" >nul
if not errorlevel 1 (
  set "FAIL_MESSAGE=%EDITOR_PORT%端口已被占用。请先关闭已有Payoff Editor或释放该端口。"
  goto :fail
)

start "Payoff SVG Editor Server" /D "%EDITOR_DIR%" "%ComSpec%" /k ""%NODE_EXE%" "%EDITOR_DIR%app\server.mjs""

for /L %%I in (1,1,30) do (
  powershell -NoProfile -Command "try { $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:%EDITOR_PORT%/api/products; if ($response.StatusCode -eq 200) { exit 0 } } catch {} ; exit 1" >nul 2>nul
  if not errorlevel 1 goto :ready
  timeout /t 1 /nobreak >nul
)

set "FAIL_MESSAGE=服务启动超时。服务窗口中可能有具体错误；请检查后重试。"
goto :fail

:ready
start "" "http://127.0.0.1:%EDITOR_PORT%"
endlocal
exit /b 0

:fail
echo.
echo Payoff Editor启动失败：%FAIL_MESSAGE%
pause
endlocal
exit /b 1
