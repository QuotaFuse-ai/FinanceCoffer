@echo off
rem Stop only the API process started by start-local.cmd.
rem Set PYTHON to a Python 3.11+ interpreter. Machine-specific settings belong
rem in scripts\local-env.local.cmd, which is gitignored.
setlocal
if exist "%~dp0local-env.local.cmd" call "%~dp0local-env.local.cmd"
if "%PYTHON%"=="" (
  echo ERROR: PYTHON is not set. Point it at a Python 3.11+ interpreter.
  exit /b 2
)
"%PYTHON%" "%~dp0local_runner.py" stop
exit /b %ERRORLEVEL%
