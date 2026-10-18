#!/usr/bin/env bash
# Durability check on an ISOLATED compose project (never the dev database or its volumes).
# Usage: DOCKER_CONTEXT=... scripts/persistence_check.sh [project] [port]
set -euo pipefail
P=${1:-cfpersisttest}; PORT=${2:-8099}; URL=http://127.0.0.1:$PORT
dc() { CONTRAFLOW_PORT=$PORT docker-compose -p "$P" "$@"; }
wait_up() { for _ in $(seq 1 90); do curl -sf "$URL/api/health" >/dev/null && return 0; sleep 2; done; echo "app not up"; exit 1; }
stats() { curl -s "$URL/api/memory" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['stats'], sort_keys=True), d['status'].get('replayed_ops'))"; }
wait_up
curl -s -X POST "$URL/api/agent" -H 'Content-Type: application/json' \
  -d '{"message":"What if Kotturpuram bridge were closed?","scenario":{"rule":"B"},"mode":"deterministic"}' >/dev/null
echo "after write:           $(stats)"
dc restart >/dev/null 2>&1; wait_up;                       echo "after restart:         $(stats)"
dc up -d --force-recreate >/dev/null 2>&1; wait_up;        echo "after force-recreate:  $(stats)"
dc down >/dev/null 2>&1; dc up -d >/dev/null 2>&1; wait_up; echo "after down + up:       $(stats)"
# graph lost but journal kept -> replay
dc exec -T falkordb redis-cli GRAPH.DELETE contraflow_memory >/dev/null
dc restart app >/dev/null 2>&1; wait_up;                   echo "after graph deleted:   $(stats)   (replayed ops shown last)"
curl -s -X POST "$URL/api/agent" -H 'Content-Type: application/json' \
  -d '{"message":"Does our earlier finding about Kotturpuram still hold?","scenario":{"rule":"B"},"mode":"deterministic"}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); f=[s['result'] for s in r['steps'] if s.get('tool')=='recall_memory'][0]['findings']; print('recall:', [(x['kind'], x.get('still_applies')) for x in f])"
