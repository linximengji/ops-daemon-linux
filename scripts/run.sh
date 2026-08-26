#!/usr/bin/env bash
# ops-daemon 由 systemd 管理生命周期，此脚本仅做转发
exec sudo systemctl start ops-daemon
