@echo off
setlocal
if not exist "[local-path]\ruanjian\embedding" mkdir "[local-path]\ruanjian\embedding"
cd /d "[local-path]\FinanceCoffer"
"[local-path]\ruanjian\venv\Scripts\python.exe" "[local-path]\FinanceCoffer\scripts\embedding_server.py" --host 127.0.0.1 --port 8001 >> "[local-path]\ruanjian\embedding\embedding.log" 2>&1
