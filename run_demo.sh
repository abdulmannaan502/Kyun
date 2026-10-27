#!/usr/bin/env sh
# Kyun demo: installs Flask, then opens the web UI on the prebuilt psf/requests graph.
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
echo "Open http://127.0.0.1:8000 in your browser (Ctrl+C to stop)"
python3 -m kyun.cli serve data/requests.json.gz --port 8000
