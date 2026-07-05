#!/usr/bin/env bash
# Setup ops-daemon systemd service + timer
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_PATH=$(command -v python3 || echo "/usr/bin/python3")

# Generate service file
cat > "$PROJECT_ROOT/scripts/ops-daemon.service" <<SERVICEEOF
[Unit]
Description=ops-daemon — system health check and auto-repair
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON_PATH -m ops_daemon.main
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF

# Generate check service
cat > "$PROJECT_ROOT/scripts/ops-daemon-check.service" <<CHECKSERVICEEOF
[Unit]
Description=ops-daemon heartbeat check

[Service]
Type=oneshot
User=$USER
ExecStart=$PROJECT_ROOT/scripts/check-daemon.sh
CHECKSERVICEEOF

# Generate check timer
cat > "$PROJECT_ROOT/scripts/ops-daemon-check.timer" <<CHECKTIMEREOF
[Unit]
Description=Run check-daemon every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
CHECKTIMEREOF

# Install systemd service
echo "Installing ops-daemon.service..."
sudo cp "$PROJECT_ROOT/scripts/ops-daemon.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ops-daemon
sudo systemctl start ops-daemon

# Install check timer
echo "Installing check timer..."
sudo cp "$PROJECT_ROOT/scripts/ops-daemon-check.service" /etc/systemd/system/
sudo cp "$PROJECT_ROOT/scripts/ops-daemon-check.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ops-daemon-check.timer
sudo systemctl start ops-daemon-check.timer

echo ""
echo "ops-daemon installed and running."
echo "  systemctl status ops-daemon"
echo "  systemctl status ops-daemon-check.timer"
echo ""
