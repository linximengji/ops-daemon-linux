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

**ops-daemon** — a Windows background daemon that periodically checks system health and writes state to JSON files. Built on `agent-core` (local package at `D:\ClaudeProjects\agent-core`, installed via `pip install -e`).

### Core loop (`main.py`)

Async loop driven by `BaseDaemon.run()` — every 60s (`check_interval` in `config.yaml`) runs all registered checks, writes results to `data/working/latest.json`. Exit via `.stop` marker file (created by `stop.ps1`).

**响应式 sleep**: 60s 间隔被拆为 5s 片段，每段检测 `.stop` 标记。响应延迟从 60s 降至 ~5s（`agent_core/base.py:run()`）。

Unhandled exceptions inside `daemon.run()` are caught by `BaseException` handler (`main.py:436`), logged to episodic before cleanup. Per-check exceptions are already isolated by `BaseDaemon._run_checks()`.

### Lifecycle management (单一权威守护)

**只有一个 Task Scheduler 任务管理 daemon 生命周期，无竞争式 watchdog。**

```
Task Scheduler (\ClaudeProjects\ops-daemon)
├── 触发器 A: AtLogOn       → 开机登录时启动 daemon
└── 触发器 B: 每 10 分钟     → check-daemon.ps1 检测 heartbeat
                                  ├─ ≤180s 新鲜 → exit 0 (noop)
                                  └─ 过期/缺失  → kill + spawn daemon
```

- `scripts/check-daemon.ps1` — **唯一入口**。检测 heartbeat，过期则重启 daemon。Task Scheduler 调度，天然串行无竞态。
- `scripts/run.ps1` — 手动启动（不依赖 Task Scheduler），用于调试。
- `scripts/stop.ps1` — 写 `.stop` marker，等 75s 优雅退出。不依赖 `Stop-Process`（被 CC hook 拦截）。超时后提示用户 `! taskkill /F /PID`。
- `scripts/setup-tasks.ps1` — 注册 Scheduled Task（管理员权限运行一次）。
- `scripts/restart-clean.ps1` — 紧急全清理（绕过 CC Hook）。

**核心原则**：不设并发守护者。Task Scheduler 是唯一有权重启 daemon 的实体。Daemon 自身不做自我重启。

### Storage (file-based, no DB)

All under `data/`:

- **working/** — `latest.json`, the current snapshot of all checks (overwritten each cycle)
- **episodic/** — `YYYY-MM-DD.jsonl`, append-only event log (daemon start/stop, proxy down, disk/cpu/memory warnings, crash traces)
- **baseline/** — `{metric}.json`, sliding window of last 168 values per metric
- **alerts/** — `history.jsonl`, fired alerts with cooldown dedup

### MCP server (`mcp_server.py`)

Standalone FastMCP server that reads from the same `data/` directory. Exposes `status()` (latest.json) and `recent_events(hours)` (episodic log).

### agent-core dependency

Shared framework at `D:\ClaudeProjects\agent-core`, installed as editable (`agent-core @ file:///D:/ClaudeProjects/agent-core`). Key classes:

- **BaseDaemon** — async main loop, check registry, stop marker graceful exit
- **StateStore** — three-zone file storage (working + episodic + baseline + alerts)
- **BaselineEngine** — sliding window (median/stdev) anomaly detection
- **AlertManager** — severity-based alerting with per-key cooldown
- **Scheduler** — threading-based interval scheduler
- **build_mcp()** — FastMCP builder exposing `status` and `recent_events` tools

### Config (`config.yaml`)

Controls check enable/disable, thresholds (disk warn 85%/critical 90%, CPU warn 80%, memory warn 85%), proxy host/port/timeout, process names to monitor, alert cooldown (30 min).
