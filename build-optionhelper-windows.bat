@echo off
setlocal EnableExtensions
rem One-click Windows v1.0.0 local-candidate build. It writes only to
rem result\windows-candidate and never replaces macOS dist or versions\v1.0.0.
set "STATUS=0"
set "ROOT=%~dp0"
set "VERSION=%OPTIONHELPER_VERSION%"
if not defined VERSION set "VERSION=v1.0.0"

if /I not "%OS%"=="Windows_NT" (
  echo This build must run on a Windows machine.
  set "STATUS=1"
  goto :done
)

if not exist "%ROOT%assets\icons\optionhelper-app-icon-tile-light.ico" (
  echo Controlled Windows application icon is missing.
  set "STATUS=1"
  goto :done
)

where powershell >nul 2>&1
if errorlevel 1 (
  echo Windows PowerShell is required to verify embedded EXE icons.
  set "STATUS=1"
  goto :done
)

where dotnet >nul 2>&1
if errorlevel 1 (
  echo dotnet SDK 8 is required.
  set "STATUS=1"
  goto :done
)
set "DOTNET8="
for /f "delims=" %%S in ('dotnet --list-sdks 2^>nul ^| findstr /B /C:"8."') do set "DOTNET8=%%S"
if not defined DOTNET8 (
  echo dotnet SDK 8 is required.
  set "STATUS=1"
  goto :done
)

if not defined OPTIONHELPER_PYTHON (
  echo 必须先由用户显式选择OPTIONHELPER_PYTHON；构建器不得自动选择候选环境。
  echo 可选解释器仅供选择；枚举过程不会运行候选解释器：
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%packaging\list_python_environments.ps1"
  echo Set OPTIONHELPER_PYTHON to one absolute python.exe path and run again.
  set "STATUS=1"
  goto :done
)
set "PYTHON_BIN=%OPTIONHELPER_PYTHON%"
call :require_absolute "%PYTHON_BIN%"
if errorlevel 1 (
  set "STATUS=1"
  goto :done
)

if not exist "%PYTHON_BIN%" (
  echo Python does not exist or is not executable: %PYTHON_BIN%
  set "STATUS=1"
  goto :done
)
if exist "%PYTHON_BIN%\NUL" (
  echo Python path must point to a file: %PYTHON_BIN%
  set "STATUS=1"
  goto :done
)

echo OptionHelper Windows candidate build
echo [1/3] Checking runtime dependencies...
"%PYTHON_BIN%" "%ROOT%packaging\skill\environment_check.py" --requirements "%ROOT%core\requirements.lock" --check-dependencies
if errorlevel 1 (
  echo Python dependencies do not match core\requirements.lock.
  set "STATUS=1"
  goto :done
)
echo [2/3] Checking packaging tools...
"%PYTHON_BIN%" "%ROOT%packaging\skill\environment_check.py" --requirements "%ROOT%packaging\build-requirements.lock" --check-dependencies
if errorlevel 1 (
  echo Python build dependencies do not match packaging\build-requirements.lock.
  set "STATUS=1"
  goto :done
)

echo [3/3] Building Skill and Windows candidate. Detailed stage progress follows.
"%PYTHON_BIN%" "%ROOT%packaging\build_current.py" --version "%VERSION%" --platform windows
set "STATUS=%ERRORLEVEL%"
:done
if "%STATUS%"=="0" (
  echo Build succeeded. Latest Skill and Windows candidate are ready.
) else (
  echo Build failed. No new delivery was published. See the failed stage above.
)
pause
exit /b %STATUS%

:require_absolute
set "CANDIDATE=%~1"
if "%CANDIDATE:~0,2%"=="\\" exit /b 0
if not "%CANDIDATE:~1,1%"==":" goto :not_absolute
if "%CANDIDATE:~2,1%"=="\" exit /b 0
if "%CANDIDATE:~2,1%"=="/" exit /b 0
:not_absolute
echo OPTIONHELPER_PYTHON must be an absolute python.exe path.
exit /b 1
