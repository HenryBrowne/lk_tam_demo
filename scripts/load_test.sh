#!/usr/bin/env bash
# Wrap `lk load-test` to generate portfolio-scale room / participant / connection-
# quality traffic on the LiveKit Cloud project.
#
# NOTE: this only lands rows in the `events` table if `python -m receiver.app` is
# running AND reachable from LiveKit Cloud (a public tunnel + the project's webhook
# pointed at https://<tunnel>/livekit/webhook). Without that it still exercises the
# SFU at scale for a realism demo, but the DB sees nothing.
#
# For the Phase 3 checkpoint you do NOT need this - scripts/simulate_accounts.py
# populates the DB. This is here to show how real portfolio-scale load would be
# generated.
#
# Usage:
#   scripts/load_test.sh <account_id> [publishers] [subscribers] [duration]
#   PUBLISHERS=8 SUBSCRIBERS=40 DURATION=3m scripts/load_test.sh globex
set -euo pipefail

ACCOUNT="${1:-loadtest}"
PUBLISHERS="${2:-${PUBLISHERS:-5}}"
SUBSCRIBERS="${3:-${SUBSCRIBERS:-20}}"
DURATION="${4:-${DURATION:-2m}}"

# Load .env if present (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a; source "${ENV_FILE}"; set +a
fi

if ! command -v lk >/dev/null 2>&1; then
  echo "error: 'lk' (the LiveKit CLI) is not on PATH." >&2
  echo "install it: https://docs.livekit.io/home/cli/   (e.g. 'brew install livekit-cli'," >&2
  echo "or download from https://github.com/livekit/livekit-cli/releases)" >&2
  exit 1
fi

: "${LIVEKIT_URL:?set LIVEKIT_URL (see .env.example)}"
: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET}"

ROOM="loadtest-${ACCOUNT}-$(date +%s)"
echo "lk load-test  room=${ROOM}  publishers=${PUBLISHERS}  subscribers=${SUBSCRIBERS}  duration=${DURATION}"
echo "(rooms are tagged to account '${ACCOUNT}' via --room-metadata)"

exec lk load-test \
  --url "${LIVEKIT_URL}" \
  --api-key "${LIVEKIT_API_KEY}" \
  --api-secret "${LIVEKIT_API_SECRET}" \
  --room "${ROOM}" \
  --room-metadata "{\"account_id\":\"${ACCOUNT}\"}" \
  --publishers "${PUBLISHERS}" \
  --subscribers "${SUBSCRIBERS}" \
  --duration "${DURATION}"
