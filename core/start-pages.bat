@echo off
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%OPTIONHELPER_PROJECT_ROOT%"
if not defined PROJECT_ROOT (
  echo 请设置OPTIONHELPER_PROJECT_ROOT为Skill安装目录外的项目运行目录。 1>&2
  exit /b 1
)
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
for %%I in ("%SCRIPT_DIR%") do set "SKILL_ROOT=%%~fI"
if /I "%PROJECT_ROOT%"=="%SKILL_ROOT%" (
  echo OPTIONHELPER_PROJECT_ROOT不得位于Skill安装目录内。 1>&2
  exit /b 1
)
rem A released Skill is read-only.  The readiness gate and the module Host
rem must use the same project-owned Store roots.
set "RUNTIME_ROOT=%PROJECT_ROOT%\.optionhelper\runtime"
set "DATA_ROOT=%PROJECT_ROOT%\data"
set "RESULT_ROOT=%PROJECT_ROOT%\result"
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%"
if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%"
if not exist "%RESULT_ROOT%" mkdir "%RESULT_ROOT%"
set "OPTIONHELPER_RUNTIME_ROOT=%RUNTIME_ROOT%"
set "OPTIONHELPER_DATA_ROOT=%DATA_ROOT%"
set "OPTIONHELPER_RESULT_ROOT=%RESULT_ROOT%"
set "STATE_FILE=%RUNTIME_ROOT%\python-path"
if "%~1"=="--check-environment" set "CHECK_ENV=1"
set "MODULE_NAME=%~1"
if "%MODULE_NAME%"=="" set "MODULE_NAME=payoffer"

if defined OPTIONHELPER_PYTHON (
  set "PYTHON_BIN=%OPTIONHELPER_PYTHON%"
) else (
  if exist "%STATE_FILE%" set /p "PYTHON_BIN=" < "%STATE_FILE%"
  if not defined PYTHON_BIN (
    setlocal EnableDelayedExpansion
    set /a CANDIDATE_COUNT=0
    if defined CONDA_PREFIX call :add_candidate "%CONDA_PREFIX%\python.exe"
    if defined CONDA_EXE (
      for %%I in ("%CONDA_EXE%") do set "CONDA_BASE=%%~dpI.."
      for %%P in ("!CONDA_BASE!\envs\*\python.exe") do call :add_candidate "%%~fP"
    )
    if !CANDIDATE_COUNT! EQU 0 (
      echo 未发现可选conda Python环境。请先激活所需环境，或设置OPTIONHELPER_PYTHON。 1>&2
      endlocal
      exit /b 1
    )
    echo 请选择本次项目工作流使用的Python解释器：
    for /L %%I in (1,1,!CANDIDATE_COUNT!) do call echo   %%I^) %%CANDIDATE_%%I%%
    set /p "PYTHON_CHOICE=输入编号："
    for /f "delims=0123456789" %%I in ("!PYTHON_CHOICE!") do set "PYTHON_CHOICE="
    if not defined PYTHON_CHOICE goto :invalid_python_choice
    if !PYTHON_CHOICE! LSS 1 goto :invalid_python_choice
    if !PYTHON_CHOICE! GTR !CANDIDATE_COUNT! goto :invalid_python_choice
    for %%I in (!PYTHON_CHOICE!) do set "PYTHON_BIN=!CANDIDATE_%%I!"
    endlocal & set "PYTHON_BIN=!PYTHON_BIN!" & set "PERSIST_SELECTION=1"
  )
)
call :require_absolute "%PYTHON_BIN%"
if errorlevel 1 exit /b 1
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
if defined PERSIST_SELECTION (
  for %%I in ("%STATE_FILE%") do if not exist "%%~dpI" mkdir "%%~dpI"
  > "%STATE_FILE%" echo %PYTHON_BIN%
)
"%PYTHON_BIN%" "%CHECKER%" --requirements "%SCRIPT_DIR%requirements.lock" --check-readiness --skill-root "%SKILL_ROOT%" --project-root "%PROJECT_ROOT%" --data-root "%DATA_ROOT%" --result-root "%RESULT_ROOT%" --runtime-root "%RUNTIME_ROOT%"
if errorlevel 1 exit /b %errorlevel%

if defined CHECK_ENV (
  "%PYTHON_BIN%" "%SCRIPT_DIR%module_host.py" --list --project-root "%PROJECT_ROOT%"
  exit /b %errorlevel%
)
"%PYTHON_BIN%" "%SCRIPT_DIR%module_host.py" --module "%MODULE_NAME%" --project-root "%PROJECT_ROOT%"
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

:add_candidate
set "CANDIDATE=%~1"
if not exist "%CANDIDATE%" exit /b 0
if exist "%CANDIDATE%\NUL" exit /b 0
for /L %%I in (1,1,!CANDIDATE_COUNT!) do if /I "!CANDIDATE_%%I!"=="%CANDIDATE%" exit /b 0
set /a CANDIDATE_COUNT+=1
set "CANDIDATE_!CANDIDATE_COUNT!=%CANDIDATE%"
exit /b 0

:invalid_python_choice
echo Python选择无效。 1>&2
endlocal
exit /b 1
