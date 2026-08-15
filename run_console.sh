#!/usr/bin/env bash
# First Pass — start the operator console.
#
# PREREQUISITES (one-time setup, see frontend/README.md):
#   pip install -r frontend/requirements.txt
#
# USAGE:
#   ./run_console.sh              # default port 8080
#   PORT=9000 ./run_console.sh    # custom port
#
# The pipeline runs in the same process as the web server.
# Ensure all required environment variables are set in .env before starting.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
echo "Starting First Pass operator console on http://localhost:${PORT}"
exec .venv/bin/uvicorn frontend.app:app --host 0.0.0.0 --port "${PORT}"
