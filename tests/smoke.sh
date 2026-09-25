#!/bin/bash
# End-to-end check of the local plant with no credentials. Run from the repo root:
#   tests/smoke.sh            (builds images; KEEP=1 leaves the stack running afterwards)
# Uses its own compose project name and state dir, so it doesn't touch a plant you already have.
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=plant-smoke
STATE="$(mktemp -d "${TMPDIR:-/tmp}/plant-smoke.XXXX")"
# Point compose's ./state bind mounts at the temp dir via an override file.
OVR="$STATE/override.yaml"
cat > "$OVR" <<YAML
services:
  init:    {volumes: ["$STATE/s:/state", "./tools/init.py:/tools/init.py:ro"]}
  logger:
    env_file: !override [{path: "$STATE/s/logger.env", required: true}]
    volumes: ["$STATE/s/logger/data:/data"]
    ports: !override ["127.0.0.1:18081:8080"]
  scaffold:
    volumes: !override ["$STATE/s/scaffold:/run/plant:ro", "scaffold-home:/home/agent"]
    environment: {BROWSER_LOGGER_URL: "http://127.0.0.1:18081"}
  console: {ports: !override ["127.0.0.1:18080:8080"]}
  gpu:     {env_file: !override [{path: "$STATE/s/gpu.env", required: true}]}
YAML
mkdir -p "$STATE/s"
dc() { docker compose -f compose.yaml -f "$OVR" "$@"; }
cleanup() {
  status=$?
  if [ $status -ne 0 ]; then echo "--- logs"; dc logs --tail 40 || true; fi
  if [ -z "${KEEP:-}" ]; then dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$STATE" 2>/dev/null || true
  else echo "left running; state in $STATE; stop with: COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME docker compose -f compose.yaml -f $OVR down -v"; fi
  exit $status
}
trap cleanup EXIT
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; exit 1; }

echo "build + init"
[ -n "${NO_BUILD:-}" ] || dc build -q
dc run --rm init >/dev/null
dc up -d --wait --wait-timeout 180 logger gpu scaffold console

L=http://127.0.0.1:18081
TOKEN="$(cat "$STATE/s/scaffold/read_token")"
sx() { dc exec -T scaffold bash -lc "$1"; }

echo "logger"
curl -fsS "$L/healthz" >/dev/null && pass "healthz"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$L/v1/entries")" = 401 ] && pass "entries need the read token"

echo "scaffold"
for _ in $(seq 1 30); do sx 'curl -fs 127.0.0.1:3300/_tap/status' >/dev/null 2>&1 && break; sleep 1; done
sx 'curl -fs 127.0.0.1:3300/_tap/status' | grep -q '"sent": [1-9]' && pass "tap registered and delivered tap_start"
sx 'curl -s -m 5 https://pypi.org >/dev/null' && fail "scaffold reached the internet" || pass "no internet from the scaffold"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/)
[ "$code" = 401 ] && pass "console asks for a password" || fail "console returned $code"
pw="$(cat "$STATE/s/scaffold/console_password")"
curl -fsS -u "lucid:$pw" http://127.0.0.1:18080/api/status | grep -q '"logger_reach": {"ok": true' && pass "dashboard reaches the logger"

echo "ssh gateway"
for _ in $(seq 1 30); do
  curl -s "$L/v1/info" | grep -q '"probe": {[^}]*"ok": true' && break; sleep 1
done
out="$(sx 'ssh -o BatchMode=yes gpu nvidia-smi')" && echo "$out" | grep -q FAKE-H100 && pass "ssh gpu nvidia-smi through the gateway"
sx 'ssh -o BatchMode=yes gpu "exit 7"' && fail "exit status lost" || [ $? = 7 ] && pass "exit status passes through"
sx 'ssh -o BatchMode=yes -tt gpu' </dev/null >/dev/null 2>&1 && fail "interactive shell allowed" || pass "interactive shell refused"
sx 'ssh -o BatchMode=yes -o UserKnownHostsFile=~/.ssh/known_hosts_lucid -i ~/.ssh/id_ed25519 -p 2222 nosuch@logger true' >/dev/null 2>&1 && fail "unknown target allowed" || pass "unknown target refused"

echo "model gateway (no Tinfoil key: expect an error reply, logged by tap and gateway)"
sx 'curl -s -m 60 -o /dev/null -H "Authorization: Bearer $(cat ~/.config/tap/model_token)" -H "Content-Type: application/json" \
     -d "{\"model\":\"gpt-oss-120b\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" 127.0.0.1:3300/v1/chat/completions' || true
[ "$(sx 'curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer wrong" http://logger:8080/v1/model/models')" = 401 ] \
  && pass "model gateway rejects a bad token"
sleep 3

echo "verify the log from outside"
python3 -c 'import cryptography' 2>/dev/null || pip install -q cryptography
python3 tools/verify_log.py --logger "$L" --token "$TOKEN" --pins "$STATE/pins.json"
kinds="$(curl -s -H "Authorization: Bearer $TOKEN" "$L/v1/entries" | python3 -c '
import json, sys; print(" ".join(sorted({json.loads(e["payload"])["kind"] for e in json.load(sys.stdin)["entries"]})))')"
for k in tap_start ssh_exec ssh_result ssh_refused model_call; do
  [[ " $kinds " == *" $k "* ]] && pass "log has $k" || fail "log missing $k (has: $kinds)"
done

echo "tamper check"
curl -s "$L/v1/info" > "$STATE/info.json"
curl -s -H "Authorization: Bearer $TOKEN" "$L/v1/entries" > "$STATE/entries.json"
python3 - "$STATE" <<'PY'
import json, sys, copy
sys.path.insert(0, "tools")
from verify_log import verify
d = sys.argv[1]
info, entries = json.load(open(f"{d}/info.json")), json.load(open(f"{d}/entries.json"))["entries"]
assert not verify(info, entries), verify(info, entries)
e = copy.deepcopy(entries); e[1]["payload"] = e[1]["payload"].replace('"', "'", 1)
assert verify(info, e), "edited payload not caught"
e = copy.deepcopy(entries); del e[1]
assert verify(info, e), "deleted entry not caught"
e = copy.deepcopy(entries); e[0]["received_at"] = "2000-01-01T00:00:00.000+00:00"
assert verify(info, e), "edited timestamp not caught"
print("  ok    edited payload, deleted entry and edited timestamp are all caught")
PY
echo "all checks passed"
