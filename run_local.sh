#!/usr/bin/env bash
# Local run: reads .env, keeps state in ./state and logs in ./logs.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "create .env first (cp .env.example .env)" >&2; exit 1; }
set -a; . ./.env; set +a
exec python3 monitor.py "$@"
