@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
rem One-click Windows v1.0.0 local-candidate build. It writes only to
rem result\windows-candidate and never replaces macOS dist or versions\v1.0.0.
set "STATUS=0"
set "ROOT=%~dp0"
set "VERSION=%OPTIONHELPER_VERSION%"
if not defined VERSION set "VERSION=v1.0.0"
set "LOCAL_PYTHON_FILE=%ROOT%.optionhelper\runtime\build-python-path"
set "PYTHON_SELECTION_SOURCE="

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

if defined OPTIONHELPER_PYTHON (
  set "PYTHON_SELECTION_SOURCE=environment"
) else if exist "%LOCAL_PYTHON_FILE%" (
  set /p "OPTIONHELPER_PYTHON=" < "%LOCAL_PYTHON_FILE%"
  if not defined OPTIONHELPER_PYTHON (
    echo 本机保存的Python解释器已失效，将重新列出候选环境。
  ) else (
    if exist "!OPTIONHELPER_PYTHON!" (
      if exist "!OPTIONHELPER_PYTHON!\NUL" (
        echo 本机保存的Python解释器已失效，将重新列出候选环境。
        set "OPTIONHELPER_PYTHON="
      ) else (
        set "PYTHON_SELECTION_SOURCE=local"
        echo 已使用本机保存的Python解释器：!OPTIONHELPER_PYTHON!
      )
    ) else (
      echo 本机保存的Python解释器已失效，将重新列出候选环境。
      set "OPTIONHELPER_PYTHON="
    )
  )
)

if not defined OPTIONHELPER_PYTHON (
  rem 不得自动选择Python；以下枚举只用于用户选择。
  echo 请选择本次构建使用的Python解释器。
  echo 可选解释器仅供选择；枚举过程不会运行候选解释器：
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%packaging\list_python_environments.ps1"
  set /a PYTHON_CANDIDATE_COUNT=0
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%packaging\list_python_environments.ps1" -PathsOnly`) do (
    set /a PYTHON_CANDIDATE_COUNT+=1
    set "PYTHON_CANDIDATE_!PYTHON_CANDIDATE_COUNT!=%%P"
  )
  if !PYTHON_CANDIDATE_COUNT! EQU 0 (
    echo 未找到可选择的Python解释器。请安装Python3.11或更高版本后重试。
    set "STATUS=1"
    goto :done
  )
  :choose_python
  set "PYTHON_CHOICE="
  set /p "PYTHON_CHOICE=请输入序号后按回车（直接回车取消）： "
  if not defined PYTHON_CHOICE (
    echo 未选择Python解释器，构建已取消。
    set "STATUS=1"
    goto :done
  )
  for /f "delims=0123456789" %%Q in ("!PYTHON_CHOICE!") do (
    echo 请输入1到!PYTHON_CANDIDATE_COUNT!之间的序号。
    goto :choose_python
  )
  if !PYTHON_CHOICE! LSS 1 (
    echo 请输入1到!PYTHON_CANDIDATE_COUNT!之间的序号。
    goto :choose_python
  )
  if !PYTHON_CHOICE! GTR !PYTHON_CANDIDATE_COUNT! (
    echo 请输入1到!PYTHON_CANDIDATE_COUNT!之间的序号。
    goto :choose_python
  )
  for %%Q in (!PYTHON_CHOICE!) do set "OPTIONHELPER_PYTHON=!PYTHON_CANDIDATE_%%Q!"
  set "PYTHON_SELECTION_SOURCE=interactive"
  echo 已选择Python解释器：!OPTIONHELPER_PYTHON!
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
if /I "%PYTHON_SELECTION_SOURCE%"=="environment" call :persist_python "%PYTHON_BIN%"
if /I "%PYTHON_SELECTION_SOURCE%"=="interactive" call :persist_python "%PYTHON_BIN%"

echo OptionHelper Windows candidate build
echo [1/3] Checking runtime dependencies...
"%PYTHON_BIN%" "%ROOT%packaging\skill\environment_check.py" --requirements "%ROOT%core\requirements.lock" --check-dependencies
if errorlevel 1 (
  echo Python dependencies do not match core\requirements.lock.
  echo If you confirm installation, run:
  echo   "%PYTHON_BIN%" -m pip install -r "%ROOT%core\requirements.lock" --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
  set "STATUS=1"
  goto :done
)
echo [2/3] Checking packaging tools...
"%PYTHON_BIN%" "%ROOT%packaging\skill\environment_check.py" --requirements "%ROOT%packaging\build-requirements.lock" --check-dependencies
if errorlevel 1 (
  echo Python build dependencies do not match packaging\build-requirements.lock.
  echo If you confirm installation, run:
  echo   "%PYTHON_BIN%" -m pip install -r "%ROOT%packaging\build-requirements.lock" --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
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

:persist_python
for %%I in ("%LOCAL_PYTHON_FILE%") do if not exist "%%~dpI" mkdir "%%~dpI"
> "%LOCAL_PYTHON_FILE%" echo %~1
echo 已将本次选择保存为本机设置：%LOCAL_PYTHON_FILE%
exit /b 0
