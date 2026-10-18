#!/bin/bash
# No command in this script enables Funnel. Public activation requires a separate explicit approval.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$HOME/Library/Application Support/ContraflowPublic/deployment.env"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
compose() { docker-compose --env-file "$CFG" -f "$ROOT/deploy_public/compose.yml" "$@"; }
case "${1:-status}" in
  build) compose up --build -d ;;
  start) compose up -d ;;
  stop) compose stop ;;
  restart) compose restart app ;;
  status) compose ps; curl --fail --max-time 4 -sS http://127.0.0.1:8008/api/health; echo ;;
  logs) compose logs --tail=80 app ;;
  health) curl --fail --max-time 4 -sS http://127.0.0.1:8008/api/health ;;
  funnel-status) "$TS" funnel status --json ;;
  *) echo 'Usage: manage.sh build|start|stop|restart|status|logs|health|funnel-status'; exit 2 ;;
esac
