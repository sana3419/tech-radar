#!/usr/bin/env bash
# supervisor loop for nohup usage (systemd users: Restart=always does this)
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/techradar bot
  echo "$(date) bot exited with $?, restarting in 5s" >> logs/bot-supervisor.log
  sleep 5
done
