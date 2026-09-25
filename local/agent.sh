#!/bin/sh
# Launch OpenCode against Tinfoil through a local verifying tinfoil-proxy. No logger, no remote scaffold.
# Usage: TINFOIL_API_KEY=... ./agent.sh [dir]   (default: ./workspace; the dir gets a copy of opencode.json)
# With 1Password: op run --env-file=.env -- ./agent.sh   (.env holding TINFOIL_API_KEY=op://...)
# Needs tinfoil-proxy (https://github.com/tinfoilsh/tinfoil-proxy/releases; check SHA256SUMS) and opencode on PATH,
# or TINFOIL_PROXY=/path/to/tinfoil-proxy.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
DIR="${1:-$ROOT/workspace}"
PROXY="${TINFOIL_PROXY:-$(command -v tinfoil-proxy || echo "$HOME/.local/bin/tinfoil-proxy")}"
: "${TINFOIL_API_KEY:?set TINFOIL_API_KEY}"

mkdir -p "$DIR"
[ -f "$DIR/opencode.json" ] || cp "$ROOT/opencode.json" "$DIR/"

if ! curl -s -o /dev/null http://127.0.0.1:3301/; then
  echo "Starting tinfoil-proxy (verifies the enclave, pins its key)..."
  "$PROXY" > "$ROOT/.proxy.log" 2>&1 &
  PROXY_PID=$!
  trap 'kill $PROXY_PID 2>/dev/null' EXIT
  for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:3301/ && break; sleep 1; done
  curl -s -o /dev/null http://127.0.0.1:3301/ || { echo "Proxy failed to start; see $ROOT/.proxy.log"; exit 1; }
fi

cd "$DIR"
opencode
