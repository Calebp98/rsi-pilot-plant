#!/bin/sh
# Sprite: a venv under ~/dashboard; Docker image: system python3 with python3-cryptography.
PY="$HOME/dashboard/.venv/bin/python"; [ -x "$PY" ] || PY=python3
exec "$PY" "$HOME/dashboard/server.py"
