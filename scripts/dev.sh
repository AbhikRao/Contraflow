#!/usr/bin/env bash
# Start API (port 8000) and Vite dev server (port 5173). Requires FalkorDB on 6379 (infra/start_falkordb.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/uvicorn contraflow.api:app --host 127.0.0.1 --port 8000 > .api.log 2>&1 &
(cd web && npm run dev -- --host 127.0.0.1 > ../.web.log 2>&1 &)
echo "API http://127.0.0.1:8000  ·  UI http://127.0.0.1:5173"
