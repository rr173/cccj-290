#!/usr/bin/env bash
# Start a temporary local server and run both callable demo scripts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8011}"
BASE_URL="http://127.0.0.1:${PORT}"
DB_FILE="${WORKBENCH_DB:-/tmp/workbench-demo-$$.db}"
export WORKBENCH_DB="$DB_FILE"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "Starting temporary service at $BASE_URL with DB $DB_FILE"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >/tmp/workbench-demo-server.log 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    cat /tmp/workbench-demo-server.log
    exit 1
  fi
  sleep 0.5
done

curl -sf "$BASE_URL/health" >/dev/null
python3 scripts/demo_two_sessions.py "$BASE_URL"
python3 scripts/demo_three_chain.py "$BASE_URL"

echo
echo "Both demos passed. Restart/concurrency check:"
python3 scripts/verify_restart_concurrency.py
echo
echo "Limited-edition issues restart/concurrency check:"
python3 scripts/verify_issues_restart_concurrency.py
