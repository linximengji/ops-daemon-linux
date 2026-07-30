#!/usr/bin/env bash
# Check daemon status — by process name, pid file is unreliable (run.sh vs daemon pid skew)
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HEARTBEAT="$PROJECT_ROOT/data/heartbeat"

# Find running daemon process
PIDS=$(pgrep -f "ops_daemon.main" | grep -v grep || true)
if [ -z "$PIDS" ]; then
    echo "ops-daemon is NOT RUNNING"
    exit 1
fi

# Pick the first matching PID
PID=$(echo "$PIDS" | head -1)
echo "ops-daemon is RUNNING (PID $PID)"
if [ -f "$HEARTBEAT" ]; then
    LAST_HB=$(stat -c "%Y" "$HEARTBEAT" 2>/dev/null || echo "unknown")
    AGE=$(( $(date +%s) - LAST_HB ))
    echo "Heartbeat: ${AGE}s ago"
fi
exit 0
