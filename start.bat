@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ACTION=%~1"
if not defined ACTION set "ACTION=console"
set "STEP=open the bot folder"
pushd "%~dp0" || goto folder_error

if /i "%ACTION%"=="console" goto prepare
if /i "%ACTION%"=="check" goto prepare
if /i "%ACTION%"=="check-telegram" goto prepare
if /i "%ACTION%"=="preview" goto prepare
if /i "%ACTION%"=="install-task" goto prepare
echo Usage: start.bat [console ^| check ^| check-telegram ^| preview [YYYY-MM-DD] ^| install-task]
set "RESULT=2"
goto done

:prepare
set "BOT_PYTHON=.venv\Scripts\python.exe"
set "CHECK_PYTHON=import struct, sys; sys.exit(not (sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8))"
if exist "%BOT_PYTHON%" goto check_venv

set "STEP=find Python 3.13 x64"
py -3.13-64 -c "%CHECK_PYTHON%" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto create_with_launcher
python -c "%CHECK_PYTHON%" >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo Install Python 3.13 x64 and enable the Python launcher or add Python to PATH.
    goto failed
)
set "STEP=create .venv"
echo Creating .venv ...
python -m venv .venv
if not "%ERRORLEVEL%"=="0" goto failed
goto check_venv

:create_with_launcher
set "STEP=create .venv"
echo Creating .venv ...
py -3.13-64 -m venv .venv
if not "%ERRORLEVEL%"=="0" goto failed

:check_venv
set "STEP=check .venv"
"%BOT_PYTHON%" -c "%CHECK_PYTHON%"
if not "%ERRORLEVEL%"=="0" (
    echo The existing .venv needs a working Python 3.13 x64. Rename .venv and run again.
    goto failed
)
fc /b requirements.txt ".venv\requirements.applied" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto check_runtime
set "STEP=install Python dependencies"
echo Installing Python dependencies ...
"%BOT_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
if not "%ERRORLEVEL%"=="0" goto failed
copy /y requirements.txt ".venv\requirements.applied" >nul
if not "%ERRORLEVEL%"=="0" goto failed

:check_runtime
if /i "%ACTION%"=="check-telegram" goto run_action
set "STEP=load ONNX Runtime"
"%BOT_PYTHON%" -X utf8 -B -c "import onnxruntime"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo For a DLL load error, check Microsoft Visual C++ Redistributable x64.
    echo Microsoft download: https://aka.ms/vc14/vc_redist.x64.exe
    goto failed
)

:run_action
set "STEP=run %ACTION%"
if /i "%ACTION%"=="check" (
    "%BOT_PYTHON%" -X utf8 -B main.py --check-models
    goto check_result
)
if /i "%ACTION%"=="check-telegram" (
    "%BOT_PYTHON%" -X utf8 -B main.py --check-telegram
    goto check_result
)
if /i "%ACTION%"=="preview" (
    if "%~2"=="" (
        "%BOT_PYTHON%" -X utf8 -B main.py --preview
    ) else (
        "%BOT_PYTHON%" -X utf8 -B main.py --preview "%~2"
    )
    goto check_result
)
if /i "%ACTION%"=="install-task" (
    powershell.exe -NoProfile -File install_task.ps1
    goto check_result
)
if not exist ".env" echo If keys are not set in the environment, copy .env.example to .env and fill it in.
"%BOT_PYTHON%" -X utf8 -B main.py

:check_result
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" goto failed
goto done

:folder_error
echo Cannot open the bot folder.
if "%~1"=="" pause
exit /b 1

:failed
if not defined RESULT set "RESULT=%ERRORLEVEL%"
echo.
echo Failed to %STEP%. Exit code: %RESULT%.
if "%~1"=="" pause

:done
popd
exit /b %RESULT%
