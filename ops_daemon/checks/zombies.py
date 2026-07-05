"""MCP zombie reaper — kill orphaned MCP server processes."""
import psutil, time

EXEMPT_PATTERNS = [
    "claudetalk",
    "ops_daemon", "agent_core",
    "model_proxy", "proxy_backup",
]


def _match_cmdline(proc, patterns: list[str]) -> bool:
    try:
        cmd = " ".join(proc.cmdline()).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        return False
    return any(p.lower() in cmd for p in patterns)


async def check_zombies(cfg: dict) -> dict:
    max_age = cfg.get("max_age_minutes", 120)
    mcp_patterns = cfg.get("mcp_cmdline_patterns", [
        "p1s-mcp", "blender-mcp", "image-gen-mcp",
        "pdf-mcp", "drawio-mcp", "openscad-mcp",
    ])
    now = time.time()
    cutoff = now - max_age * 60
    killed = []
    running_fresh = []

    for proc in psutil.process_iter(attrs=["pid", "name", "create_time"]):
        if not _match_cmdline(proc, mcp_patterns):
            continue
        if _match_cmdline(proc, EXEMPT_PATTERNS):
            continue
        try:
            create_time = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        age_min = (now - create_time) / 60
        entry = {
            "pid": proc.pid, "name": proc.name(),
            "age_min": round(age_min, 1),
            "cmdline": " ".join(proc.cmdline())[:120],
        }

        if create_time < cutoff:
            try:
                proc.kill()
                killed.append(entry)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                killed.append({**entry, "kill_failed": True})
        else:
            running_fresh.append(entry)

    result = {}
    if killed:
        result["killed"] = killed
    if running_fresh:
        result["running"] = running_fresh
    if not killed and not running_fresh:
        result["status"] = "none"
    return result
