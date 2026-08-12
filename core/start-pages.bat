@echo off
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
if "%~1"=="--check-environment" set "CHECK_ENV=1"
set "MODULE_NAME=%~1"
if "%MODULE_NAME%"=="" set "MODULE_NAME=payoffer"

set "PYTHON_BIN="
if defined OPTIONHELPER_PYTHON (
  set "PYTHON_BIN=%OPTIONHELPER_PYTHON%"
  call :require_absolute "%PYTHON_BIN%"
  if errorlevel 1 exit /b 1
)
if not defined PYTHON_BIN for /f "delims=" %%P in ('where python3 2^>nul') do if not defined PYTHON_BIN set "PYTHON_BIN=%%P"
if not defined PYTHON_BIN for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_BIN set "PYTHON_BIN=%%P"
if not defined PYTHON_BIN (
  echo 未找到可用Python。请设置OPTIONHELPER_PYTHON，或安装python3。 1>&2
  exit /b 1
)
if not exist "%PYTHON_BIN%" (
  echo Python不存在或不可执行：%PYTHON_BIN% 1>&2
  exit /b 1
)
if exist "%PYTHON_BIN%\NUL" (
  echo Python路径必须指向文件：%PYTHON_BIN% 1>&2
  exit /b 1
)

set "CHECKER=%SCRIPT_DIR%environment_check.py"
if not exist "%CHECKER%" set "CHECKER=%SCRIPT_DIR%..\packaging\skill\environment_check.py"
if not exist "%CHECKER%" (
  echo 缺少环境检查器：%CHECKER% 1>&2
  exit /b 1
)
"%PYTHON_BIN%" "%CHECKER%" --requirements "%SCRIPT_DIR%requirements.lock" --check-dependencies
if errorlevel 1 exit /b %errorlevel%

if defined CHECK_ENV (
  "%PYTHON_BIN%" "%SCRIPT_DIR%module_host.py" --list
  exit /b %errorlevel%
)
"%PYTHON_BIN%" "%SCRIPT_DIR%module_host.py" --module "%MODULE_NAME%"
exit /b %errorlevel%

:require_absolute
set "CANDIDATE=%~1"
if "%CANDIDATE:~0,2%"=="\\" exit /b 0
if not "%CANDIDATE:~1,1%"==":" goto :not_absolute
if "%CANDIDATE:~2,1%"=="\" exit /b 0
if "%CANDIDATE:~2,1%"=="/" exit /b 0
:not_absolute
echo OPTIONHELPER_PYTHON必须是绝对Python路径。 1>&2
exit /b 1
