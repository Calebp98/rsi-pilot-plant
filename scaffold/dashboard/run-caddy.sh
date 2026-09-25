#!/bin/sh
# CADDYFILE: the Docker image writes a copy with the password hash filled in; on the Sprite it's edited in place.
exec "$(command -v caddy || echo "$HOME/.local/bin/caddy")" run --config "${CADDYFILE:-$HOME/dashboard/Caddyfile}" --adapter caddyfile
