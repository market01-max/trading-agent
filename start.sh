#!/bin/bash
# dashboard = foreground (Railway health checks this)
# agent     = background with auto-restart loop

echo "=== Trading Agent Startup ==="

# Agent in background with restart loop
(
  while true; do
    echo "[agent] Starting..."
    python agent.py
    echo "[agent] Exited — restarting in 10s..."
    sleep 10
  done
) &

echo "[startup] Agent loop running (PID $!)"
sleep 2

echo "[startup] Starting dashboard..."
exec python dashboard.py
