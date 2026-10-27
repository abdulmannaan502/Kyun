@echo off
REM Kyun demo: installs Flask, then opens the web UI on the prebuilt psf/requests graph.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" http://127.0.0.1:8000
python -m kyun.cli serve data\requests.json.gz --port 8000
pause
