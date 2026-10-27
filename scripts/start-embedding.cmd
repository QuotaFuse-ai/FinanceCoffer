@echo off
rem Start the local bge-m3 embedding service on 127.0.0.1:8001.
rem Machine-specific settings (interpreter, model weights, log path) belong in
rem scripts\start-embedding.local.cmd, which is gitignored.
setlocal
cd /d "%~dp0.."

if exist "%~dp0start-embedding.local.cmd" (
  call "%~dp0start-embedding.local.cmd"
)

if "%PYTHON%"=="" set "PYTHON=python"
if "%FC_EMBEDDING_MODEL_DIR%"=="" set "FC_EMBEDDING_MODEL_DIR=%CD%\var\models\bge-m3"
if "%FC_EMBEDDING_LOG%"=="" set "FC_EMBEDDING_LOG=%CD%\var\logs\embedding.log"

for %%I in ("%FC_EMBEDDING_LOG%") do set "FC_EMBEDDING_LOGDIR=%%~dpI"
if "%FC_EMBEDDING_LOGDIR:~-1%"=="\" set "FC_EMBEDDING_LOGDIR=%FC_EMBEDDING_LOGDIR:~0,-1%"
if not exist "%FC_EMBEDDING_LOGDIR%" mkdir "%FC_EMBEDDING_LOGDIR%"

"%PYTHON%" "%~dp0embedding_server.py" --host 127.0.0.1 --port 8001 --model-dir "%FC_EMBEDDING_MODEL_DIR%" >> "%FC_EMBEDDING_LOG%" 2>&1
