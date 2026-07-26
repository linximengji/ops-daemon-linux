#!/usr/bin/env bash
# Install the user-level trip-activate timer (owns pending->active trip activation).
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_DIR"

cp "$PROJECT_ROOT/scripts/trip-activate.service" "$USER_DIR/"
cp "$PROJECT_ROOT/scripts/trip-activate.timer" "$USER_DIR/"

systemctl --user daemon-reload
systemctl --user enable --now trip-activate.timer

echo ""
echo "trip-activate.timer installed and running."
echo "  systemctl --user status trip-activate.timer"
echo "  systemctl --user list-timers | grep trip"
echo ""
