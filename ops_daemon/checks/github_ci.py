"""Check GitHub CI status for all known projects. Notify Feishu on new failures."""
import os, json, time, subprocess, shutil
from pathlib import Path

PROXY_ENV = Path(os.environ.get("CLAUDE_PROJECTS", str(Path.home() / "projects"))) / "proxy" / ".env"

# Resolve gh CLI path
GH_CMD = os.environ.get("GH_PATH") or shutil.which("gh")
if not GH_CMD:
    GH_CMD = "gh"  # last resort: hope it's in PATH

# Projects to monitor — (repo, display_name, branch)
MONITORED_REPOS = [
    ("linximengji/ops-daemon", "ops-daemon", "master"),
    ("linximengji/claudetalk", "claudetalk", "main"),
    ("linximengji/memory-mcp", "memory-mcp", "master"),
    ("linximengji/semi-report", "semi-report", "master"),
    ("linximengji/p1s-mcp", "p1s-mcp", "master"),
    ("linximengji/pdf-mcp", "pdf-mcp", "master"),
    ("linximengji/image-gen-mcp", "image-gen-mcp", "master"),
]


def _read_token() -> str | None:
    if not PROXY_ENV.exists():
        return os.environ.get("GITHUB_TOKEN")
    for line in PROXY_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get("GITHUB_TOKEN")


async def _query_ci_status(repo: str, branch: str, token: str) -> dict | None:
    try:
        r = subprocess.run(
            [GH_CMD, "run", "list", "--repo", repo, "--branch", branch,
             "--limit", "1", "--json", "conclusion,databaseId,displayTitle,url"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "GH_TOKEN": token},
        )
        if r.returncode != 0 or r.stdout is None:
            return None
        runs = json.loads(r.stdout)
        if not runs:
            return None
        run = runs[0]
        return {
            "conclusion": run["conclusion"],
            "run_id": str(run["databaseId"]),
            "title": run["displayTitle"],
            "url": run["url"],
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def _load_previous(data_dir: Path) -> dict:
    path = data_dir / "github_ci_state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(data_dir: Path, state: dict):
    path = data_dir / "github_ci_state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def check_github_ci(cfg: dict, store=None) -> dict:
    token = _read_token()
    if not token:
        return {"status": "skipped", "reason": "no GITHUB_TOKEN found"}

    min_interval = cfg.get("min_interval_seconds", 900)
    data_dir = Path(cfg.get("state_dir", "."))

    prev = _load_previous(data_dir)
    last_check = prev.get("_last_check", 0)
    now = time.time()
    if now - last_check < min_interval:
        return {"status": "skipped", "reason": f"within min_interval ({min_interval}s)"}

    failures: list[dict] = []
    recoveries: list[dict] = []

    for repo, name, branch in MONITORED_REPOS:
        status = await _query_ci_status(repo, branch, token)
        if status is None:
            continue

        prev_run_id = prev.get(name, {}).get("run_id")
        prev_conclusion = prev.get(name, {}).get("conclusion")

        if status["run_id"] != prev_run_id:
            is_failure = status["conclusion"] in ("failure", "cancelled", "timed_out")
            was_failure = prev_conclusion in ("failure", "cancelled", "timed_out")

            if is_failure:
                failures.append({
                    "name": name, "repo": repo, "branch": branch,
                    "title": status["title"], "url": status["url"],
                })
            elif was_failure and status["conclusion"] == "success":
                recoveries.append({
                    "name": name, "repo": repo,
                    "title": status["title"], "url": status["url"],
                })

            prev[name] = status

    prev["_last_check"] = now
    _save_state(data_dir, prev)

    if failures:
        store.append_episodic({"type": "github_ci_failure", "items": failures})

        # Send Feishu notification
        lines = [f"**{f['name']}** ({f['repo']}, {f['branch']})" for f in failures]
        detail = "\n\n".join(lines)
        try:
            from ops_daemon.notify import notify
            await notify("CRITICAL", f"GitHub CI 失败 ({len(failures)} 项目)", detail)
        except ImportError:
            pass

    if recoveries:
        store.append_episodic({"type": "github_ci_recovery", "items": recoveries})

        rec_lines = [f"**{r['name']}**" for r in recoveries]
        try:
            from ops_daemon.notify import notify
            await notify("INFO", f"GitHub CI 已恢复 ({len(recoveries)} 项目)", "\n".join(rec_lines))
        except ImportError:
            pass

    return {
        "status": "ok",
        "detail": {
            "projects_total": len(MONITORED_REPOS),
            "failures": failures,
            "recoveries": recoveries,
        },
    }
