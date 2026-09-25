#!/bin/sh
# Browser terminal attached to tmux session "main" (created if missing). Localhost only; Caddy fronts it.
exec "$(command -v ttyd || echo "$HOME/.local/bin/ttyd")" -i 127.0.0.1 -p 7681 -b /term -W -t fontSize=13 \
  tmux new-session -A -s main -c "$HOME/workspace"
