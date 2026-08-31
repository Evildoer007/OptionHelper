@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
rem One-click Windows local-candidate build. Formal archives are never written.
set "STATUS=0"
set "ROOT=%~dp0"
set "VERSION=%OPTIONHELPER_VERSION%"
if not defined VERSION set "VERSION=v1.0.0"
set "LOCAL_PYTHON_FILE=%ROOT%.optionhelper\runtime\build-python-path"
set "PYTHON_SELECTION_SOURCE="

if /I not "%OS%"=="Windows_NT" goto :wrong_platform
where powershell >nul 2>&1
if errorlevel 1 goto :missing_powershell

if defined OPTIONHELPER_PYTHON goto :python_from_environment
if not exist "%LOCAL_PYTHON_FILE%" goto :choose_python
set /p "OPTIONHELPER_PYTHON=" < "%LOCAL_PYTHON_FILE%"
if not defined OPTIONHELPER_PYTHON goto :invalid_saved_python
if not exist "%OPTIONHELPER_PYTHON%" goto :invalid_saved_python
if exist "%OPTIONHELPER_PYTHON%\NUL" goto :invalid_saved_python
set "PYTHON_SELECTION_SOURCE=local"
echo 已使用本机保存的Python解释器：%OPTIONHELPER_PYTHON%
goto :python_selected

:python_from_environment
set "PYTHON_SELECTION_SOURCE=environment"
goto :python_selected

:invalid_saved_python
echo 本机保存的Python解释器已失效，将重新列出候选环境。
set "OPTIONHELPER_PYTHON="

:choose_python
echo 请选择本次构建使用的Python解释器。
echo 可选解释器仅供选择；枚举过程不会运行候选解释器。
echo 为避免误用环境，候选解释器不得自动选择。
set "PYTHON_SELECTION_FILE=%TEMP%\optionhelper-python-selection-%RANDOM%.txt"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%packaging\select_python_environment.ps1" -ListScript "%ROOT%packaging\list_python_environments.ps1" -OutputFile "%PYTHON_SELECTION_FILE%"
if errorlevel 1 goto :selection_failed
set /p "OPTIONHELPER_PYTHON=" < "%PYTHON_SELECTION_FILE%"
del /q "%PYTHON_SELECTION_FILE%" >nul 2>&1
if not defined OPTIONHELPER_PYTHON goto :selection_failed
set "PYTHON_SELECTION_SOURCE=interactive"
echo 已选择Python解释器：%OPTIONHELPER_PYTHON%

:python_selected
set "PYTHON_BIN=%OPTIONHELPER_PYTHON%"
call :require_absolute "%PYTHON_BIN%"
if errorlevel 1 goto :failed
if not exist "%PYTHON_BIN%" goto :missing_python
if exist "%PYTHON_BIN%\NUL" goto :missing_python
if /I "%PYTHON_SELECTION_SOURCE%"=="environment" goto :persist_python
if /I "%PYTHON_SELECTION_SOURCE%"=="interactive" goto :persist_python
goto :preflight

:persist_python
"%PYTHON_BIN%" "%ROOT%packaging\persist_build_python.py" --project-root "%ROOT%" --python "%PYTHON_BIN%"
if errorlevel 1 goto :failed
echo 已保存本机Python解释器供后续构建复用。

:preflight
"%PYTHON_BIN%" "%ROOT%packaging\app\windows\check_build_tools.py"
if errorlevel 1 goto :failed

echo OptionHelper Windows candidate build
echo [1/3] Checking runtime dependencies and locked packages...
set "CHECK_OUTPUT=%TEMP%\optionhelper-dependency-check-%RANDOM%.json"
"%PYTHON_BIN%" "%ROOT%packaging\skill\environment_check.py" --requirements "%ROOT%core\requirements.lock" --requirements "%ROOT%packaging\build-requirements.lock" --project-root "%ROOT%" --check-dependencies > "%CHECK_OUTPUT%"
if errorlevel 1 goto :dependency_failed
findstr /C:"\"status\": \"hit\"" "%CHECK_OUTPUT%" >nul 2>&1
if errorlevel 1 echo [1/3] Runtime dependency check passed.
if not errorlevel 1 echo [1/3] Runtime is ready; reusing the previous dependency check.
del /q "%CHECK_OUTPUT%" >nul 2>&1

echo Restoring both locked Agent Runtime dependency roots...
"%PYTHON_BIN%" "%ROOT%packaging\app\agent_runtime\restore_dependencies.py"
if errorlevel 1 goto :failed

echo [2/3] Runtime is ready; preparing the Windows candidate.
echo [3/3] Building Skill and Windows candidate. Detailed stage progress follows.
set "OPTIONHELPER_REQUIRE_NATIVE_RUNTIME=1"
"%PYTHON_BIN%" "%ROOT%packaging\build_current.py" --version "%VERSION%" --platform windows
if errorlevel 1 goto :failed
goto :success

:dependency_failed
echo Python dependencies do not match the locked requirements.
echo If you confirm installation, run:
echo   "%PYTHON_BIN%" -m pip install -r "%ROOT%core\requirements.lock" --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
echo   "%PYTHON_BIN%" -m pip install -r "%ROOT%packaging\build-requirements.lock" --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
type "%CHECK_OUTPUT%"
del /q "%CHECK_OUTPUT%" >nul 2>&1
goto :failed

:selection_failed
if defined PYTHON_SELECTION_FILE del /q "%PYTHON_SELECTION_FILE%" >nul 2>&1
echo 未选择Python解释器，构建已取消。
goto :failed

:wrong_platform
echo This build must run on a Windows machine.
goto :failed

:missing_powershell
echo Windows PowerShell is required.
goto :failed

:missing_python
echo Python does not exist or is not executable: %PYTHON_BIN%
goto :failed

:success
echo Build succeeded. Latest Skill and Windows candidate are ready.
set "STATUS=0"
goto :done

:failed
echo Build failed. No new delivery was published. See the failed stage above.
set "STATUS=1"
goto :done

:require_absolute
set "CANDIDATE=%~1"
if "%CANDIDATE:~0,2%"=="\\" exit /b 0
if not "%CANDIDATE:~1,1%"==":" goto :not_absolute
if "%CANDIDATE:~2,1%"=="\" exit /b 0
if "%CANDIDATE:~2,1%"=="/" exit /b 0
:not_absolute
echo OPTIONHELPER_PYTHON must be an absolute python.exe path.
exit /b 1

:done
pause
exit /b %STATUS%
