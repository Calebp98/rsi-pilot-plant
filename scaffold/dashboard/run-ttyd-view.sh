#!/bin/sh
# Read-only browser terminal for the view-only link: watches tmux session "main" but can't type into it.
# Two independent locks: ttyd runs without -W (it drops all keyboard input), and tmux attaches read-only.
# ignore-size stops a viewer's window size from resizing the session for everyone else.
# Localhost only; Caddy serves it at /view/<secret>/term/ after the dashboard server checks the secret.
exec "$(command -v ttyd || echo "$HOME/.local/bin/ttyd")" -i 127.0.0.1 -p 7682 -b /rterm -t fontSize=13 -t disableLeaveAlert=true \
  sh -c 'tmux attach-session -t main -f read-only,ignore-size || { echo "no agent session running"; sleep 5; }'
