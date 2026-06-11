#!/usr/bin/env bash
# Auto-Restart-Wrapper: hält den Bot am Leben ("läuft wie von alleine").
# Stürzt der Server ab, startet er mit Backoff neu; Strg+C beendet sauber.
#   ./start.sh           # Web-UI (server.py)
#   ./start.sh headless  # ohne UI (autopilot.py)
set -u
cd "$(dirname "$0")"

TARGET="server.py"
[ "${1:-}" = "headless" ] && TARGET="autopilot.py"

BACKOFF=5
trap 'echo; echo "Gestoppt."; exit 0' INT TERM

while true; do
  echo "[start.sh] starte python3 $TARGET ..."
  python3 "$TARGET"
  CODE=$?
  if [ $CODE -eq 0 ]; then
    echo "[start.sh] sauber beendet."; exit 0
  fi
  echo "[start.sh] Exit-Code $CODE - Neustart in ${BACKOFF}s (Strg+C zum Abbrechen)"
  sleep "$BACKOFF"
  BACKOFF=$(( BACKOFF * 2 > 300 ? 300 : BACKOFF * 2 ))
done
