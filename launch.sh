#!/usr/bin/env bash
# Dev launcher — activates venv and starts uvicorn with hot reload.
# For production, use the systemd service instead (see systemd/printbot-backend.service).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source venv/bin/activate

exec uvicorn web.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --loop asyncio \
  --reload \
  --log-level info
