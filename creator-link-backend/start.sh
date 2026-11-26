#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export WORKBENCH_DB="${WORKBENCH_DB:-$PWD/data/workbench.db}"
mkdir -p "$(dirname "$WORKBENCH_DB")"
exec python3 -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
