#!/bin/sh
# Lay out $HOME the way the Sprite has it, from the image (/opt/plant) and the keys `init` generated (/run/plant).
# Code is symlinked, so a new image takes effect on restart; keys, sessions and OpenCode state persist in $HOME.
#
# Env: LOGGER_URL (tap -> logger, e.g. http://logger:8080), BROWSER_LOGGER_URL (browser -> logger),
#      GATEWAY_HOST (SSH gateway host, default: LOGGER_URL's host), GATEWAY_PORT (default 2222),
#      SSH_TARGETS (space-separated gateway usernames to add as ssh hosts, default "gpu").
set -eu
K=/run/plant
for f in tap_ed25519.pem model_token read_token id_ed25519 id_ed25519.pub known_hosts_lucid console_password; do
  [ -f "$K/$f" ] || { echo "missing $K/$f: run 'docker compose run --rm init' first" >&2; exit 1; }
done
: "${LOGGER_URL:?set LOGGER_URL}"
GATEWAY_HOST="${GATEWAY_HOST:-$(python3 -c 'import sys,urllib.parse;print(urllib.parse.urlparse(sys.argv[1]).hostname)' "$LOGGER_URL")}"

mkdir -p ~/.config/tap ~/.config/caddy ~/.ssh ~/.local/bin ~/sessions
chmod 700 ~/.config/tap ~/.ssh
ln -sfn /opt/plant/tap ~/tap
ln -sfn /opt/plant/dashboard ~/dashboard
ln -sf /opt/plant/bin/agent ~/.local/bin/agent

install -m 600 "$K/tap_ed25519.pem" ~/.config/tap/ed25519.pem
install -m 600 "$K/model_token" ~/.config/tap/model_token
install -m 600 "$K/read_token" ~/.config/tap/read_token
python3 - "$LOGGER_URL" "${BROWSER_LOGGER_URL:-}" > ~/.config/tap/config.json <<'PY'
import json, sys
cfg = {"logger_url": sys.argv[1], "client": "scaffold"}
if sys.argv[2]:
    cfg["browser_logger_url"] = sys.argv[2]
print(json.dumps(cfg))
PY

install -m 600 "$K/id_ed25519" ~/.ssh/id_ed25519
install -m 644 "$K/id_ed25519.pub" ~/.ssh/id_ed25519.pub
install -m 644 "$K/known_hosts_lucid" ~/.ssh/known_hosts_lucid
: > ~/.ssh/config
for t in ${SSH_TARGETS:-gpu}; do
  cat >> ~/.ssh/config <<CFG
Host $t
  HostName $GATEWAY_HOST
  Port ${GATEWAY_PORT:-2222}
  User $t
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  UserKnownHostsFile ~/.ssh/known_hosts_lucid
  StrictHostKeyChecking yes
CFG
done
chmod 600 ~/.ssh/config

# Console password (user "lucid") -> bcrypt into a runtime copy of the Caddyfile.
hash="$(caddy hash-password --plaintext "$(cat "$K/console_password")")"
sed "s#BCRYPT_HASH#$hash#" /opt/plant/dashboard/Caddyfile > ~/.config/caddy/Caddyfile
export CADDYFILE="$HOME/.config/caddy/Caddyfile"

# First start: a workspace from the session template (New session replaces it with ~/sessions/<id>).
if [ ! -e ~/workspace ]; then
  mkdir ~/workspace && cp /opt/plant/dashboard/session-template/* ~/workspace/
fi

exec supervisord -c /opt/plant/supervisord.conf
