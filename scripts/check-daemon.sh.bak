#!/usr/bin/env bash
# Single authority for daemon lifecycle.
# Called by systemd timer every 10 minutes.
# Detects stale heartbeat → kills stale daemon → spawns new one.
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/data/daemon.pid"
HEARTBEAT="$PROJECT_ROOT/data/heartbeat"
STOPFILE="$PROJECT_ROOT/.stop"
STARTLOG="$PROJECT_ROOT/data/ops-daemon-start.log"
INITIALIZED="$PROJECT_ROOT/data/initialized"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$STARTLOG"; }

# Reap any .restart-* or .stop-* markers left by aborted daemon
# These are only meaningful to a running daemon — clean them up.
clean_markers() {
    find "$PROJECT_ROOT/data" -maxdepth 1 -name '.restart-*' -o -name '.stop-*' -exec rm -f {} \; 2>/dev/null || true
}

# Intentional stop?
if [ -f "$STOPFILE" ]; then
    if [ -f "$PIDFILE" ]; then
        OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            HB_AGE=999
            [ -f "$HEARTBEAT" ] && HB_AGE=$(( $(date +%s) - $(stat -c "%Y" "$HEARTBEAT" 2>/dev/null || echo 0) ))
            if [ "$HB_AGE" -gt 120 ]; then
                log ".stop present, daemon stale — force killing $OLD_PID"
                kill -9 "$OLD_PID" 2>/dev/null || true
                sleep 2
                rm -f "$PIDFILE" "$STOPFILE"
            else
                exit 0
            fi
        else
            rm -f "$STOPFILE" "$PIDFILE"
        fi
    else
        rm -f "$STOPFILE"
    fi
    clean_markers
    exit 0
fi

# Heartbeat fresh enough?
HB_OK=false
if [ -f "$HEARTBEAT" ]; then
    HB_AGE=$(( $(date +%s) - $(stat -c "%Y" "$HEARTBEAT" 2>/dev/null || echo 0) ))
    [ "$HB_AGE" -le 180 ] && HB_OK=true
fi
$HB_OK && clean_markers && exit 0

# ── Daemon is stale or missing — restart ────────────────────────────

# Kill stale daemon if PID file exists
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        log "Killing stale daemon PID $OLD_PID"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 3
        kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PIDFILE"
fi

clean_markers

# Spawn new daemon
cd "$PROJECT_ROOT"
nohup python3 -m ops_daemon.main >> "$PROJECT_ROOT/data/daemon_stdout.log" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
log "Started daemon PID $NEW_PID"

# Wait up to 10s for initialization
for i in $(seq 1 5); do
    sleep 2
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        log "FAILED: daemon PID $NEW_PID died immediately"
        rm -f "$PIDFILE"
        exit 1
    fi
    if [ -f "$HEARTBEAT" ]; then
        HB_AGE=$(( $(date +%s) - $(stat -c "%Y" "$HEARTBEAT" 2>/dev/null || echo 0) ))
        [ "$HB_AGE" -le 10 ] && { log "Daemon $NEW_PID initialized"; exit 0; }
    fi
done

if [ -f "$HEARTBEAT" ]; then
    log "Daemon $NEW_PID running (no recent heartbeat yet, may be in first check cycle)"
fi
exit 0
