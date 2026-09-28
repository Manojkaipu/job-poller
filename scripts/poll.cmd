@echo off
rem Scheduled entry point on Windows: run one poll from the repo root and log it.
cd /d "%~dp0.."
if not exist state mkdir state
echo === %date% %time% >> state\poll.log
.venv\Scripts\python.exe -m jobpoller run >> state\poll.log 2>&1
