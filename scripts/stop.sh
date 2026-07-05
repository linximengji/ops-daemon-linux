#!/usr/bin/env bash
# Graceful stop: write .stop marker and wait
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/data/daemon.pid"
STOPFILE="$PROJECT_ROOT/.stop"

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file found. Nothing to stop."
    exit 0
fi

PID=$(cat "$PIDFILE")
touch "$STOPFILE"
echo "Waiting for daemon PID $PID to exit gracefully..."
for i in $(seq 1 75); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Daemon exited gracefully after ${i}s."
        rm -f "$PIDFILE" "$STOPFILE"
        exit 0
    fi
    sleep 1
done

echo "Timeout reached. Force-killing daemon PID $PID..."
kill -9 "$PID" 2>/dev/null || true
rm -f "$PIDFILE" "$STOPFILE"
echo "Daemon force-killed."
