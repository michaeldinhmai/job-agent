@echo off
rem Daily job-agent digest — run by Windows Task Scheduler.
cd /d C:\Users\dinht\job-agent
if not exist logs mkdir logs
echo ==== %date% %time% ==== >> logs\digest.log
.venv\Scripts\python.exe -m jobagent digest >> logs\digest.log 2>&1
