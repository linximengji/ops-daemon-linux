#!/usr/bin/env bash
# Check daemon status
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/data/daemon.pid"
HEARTBEAT="$PROJECT_ROOT/data/heartbeat"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "ops-daemon is RUNNING (PID $PID)"
        if [ -f "$HEARTBEAT" ]; then
            LAST_HB=$(stat -c "%Y" "$HEARTBEAT" 2>/dev/null || echo "unknown")
            AGE=$(( $(date +%s) - LAST_HB ))
            echo "Heartbeat: ${AGE}s ago"
        fi
        exit 0
    fi
fi
echo "ops-daemon is NOT RUNNING"
exit 1
