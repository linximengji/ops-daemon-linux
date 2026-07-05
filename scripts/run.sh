#!/usr/bin/env bash
# Start ops-daemon in background
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/data/daemon.pid"
STOPFILE="$PROJECT_ROOT/.stop"

rm -f "$STOPFILE"

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
    rm -f "$PIDFILE"
fi

cd "$PROJECT_ROOT"
nohup python3 -m ops_daemon.main >> "$PROJECT_ROOT/data/daemon_stdout.log" 2>&1 &
echo $! > "$PIDFILE"
echo "ops-daemon started, PID $(cat "$PIDFILE")"
