# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run (foreground, for debugging)
python3 -m ops_daemon.main

# Run (background daemon)
./scripts/run.sh

# Stop daemon
./scripts/stop.sh

# Check status
./scripts/status.sh

# Systemd setup (run once as root)
./scripts/setup.sh

# Start MCP server
python3 -m ops_daemon.mcp_server
```

## Stopping the daemon

Write `.stop` marker file — daemon detects it within ~5s and exits gracefully:

```bash
touch .stop
# or use ./scripts/stop.sh
```

Force kill only if graceful stop times out:

```bash
kill -9 $(cat data/daemon.pid)
```

## Architecture

**ops-daemon** — a Linux monitoring-only daemon that periodically checks system health and writes state to JSON files. Built on `agent-core` (local package at `/home/ubuntu/projects/agent-core`, installed via `pip install -e`).

**核心原则：只监视，不维护。** Daemon 不做 PID 锁、不做进程管理、不做自动修复。所有子进程 (claudetalk, feishu-bridge, proxy) 由 systemd 管理生命周期。

### Core loop (`main.py`)

Async loop driven by `BaseDaemon.run()` — every 60s (`check_interval` in `config.yaml`) runs all registered checks, writes results to `data/working/latest.json`. Exit via `.stop` marker file.

**响应式 sleep**: 60s 间隔被拆为 5s 片段，每段检测 `.stop` 标记。响应延迟从 60s 降至 ~5s（`agent_core/base.py:run()`）。

### What was removed

- PID locking (flock/Named Mutex) — replaced with simple PID file write
- `_restart_watcher` — marker-based process restart loop
- Auto-repair (`REPAIR_REGISTRY` + `RepairCoordinator`) — no more automatic service restoration
- Lifecycle hooks (`_claudetalk_lifecycle`, `_repair_proxy_backup`, `_cloudflared_auto_stop`, `_proxy_auto_switch`)
- Boot-time child process spawning — all services managed by systemd
- `.restart-daemon` / `.trigger-restart` in finally block

### Storage (file-based, no DB)

All under `data/`:

- **working/** — `latest.json`, the current snapshot of all checks (overwritten each cycle)
- **episodic/** — `YYYY-MM-DD.jsonl`, append-only event log (daemon start/stop, proxy down, disk/cpu/memory warnings, crash traces)
- **baseline/** — `{metric}.json`, sliding window of last 168 values per metric
- **alerts/** — `history.jsonl`, fired alerts with cooldown dedup

### MCP server (`mcp_server.py`)

Standalone FastMCP server that reads from the same `data/` directory. Exposes `status()` (latest.json) and `recent_events(hours)` (episodic log).

### agent-core dependency

Shared framework at `/home/ubuntu/projects/agent-core`, installed as editable. Key classes:

- **BaseDaemon** — async main loop, check registry, stop marker graceful exit
- **StateStore** — three-zone file storage (working + episodic + baseline + alerts)
- **BaselineEngine** — sliding window (median/stdev) anomaly detection
- **AlertManager** — severity-based alerting with per-key cooldown
- **Scheduler** — threading-based interval scheduler
- **build_mcp()** — FastMCP builder exposing `status` and `recent_events` tools

### Config (`config.yaml`)

Controls check enable/disable, thresholds (disk warn 85%/critical 90%, CPU warn 80%, memory warn 85%), proxy host/port/timeout, process names to monitor, alert cooldown (30 min).
