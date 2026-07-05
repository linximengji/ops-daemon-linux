"""Docker Compose watchdog — ensures pact-broker + jaeger are running."""

import asyncio, json, os
from pathlib import Path

COMPOSE_DIR = Path("/home/ubuntu/projects")
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
SERVICES = ["pact-broker", "jaeger"]


async def check_compose_up(cfg: dict, store=None) -> dict:
    if not COMPOSE_FILE.exists():
        return {"status": "skipped", "detail": "docker-compose.yml not found"}

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "docker", "compose", "-f", str(COMPOSE_FILE), "ps", "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=15,
        )
        stdout, stderr = await proc.communicate()
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        return {"status": "degraded", "detail": f"docker not available: {e}"}

    if proc.returncode != 0:
        return {"status": "degraded", "detail": f"docker compose ps failed: {stderr.decode('utf-8', errors='replace').strip()[:200]}"}

    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        # All containers stopped — attempt restart
        try:
            up_proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", *SERVICES,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=60,
            )
            _, up_stderr = await up_proc.communicate()
            if up_proc.returncode != 0:
                return {"status": "degraded", "detail": f"restart all failed: {up_stderr.decode('utf-8', errors='replace').strip()[:200]}"}
            return {"status": "up", "detail": f"restarted: {', '.join(SERVICES)}"}
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            return {"status": "degraded", "detail": f"docker compose up failed: {e}"}

    running = {}
    for line in raw.strip().split("\n"):
        try:
            info = json.loads(line)
            name = info.get("Name") or info.get("Service") or ""
            state = info.get("State", "")
            running[name.replace(COMPOSE_DIR.name + "-", "").replace("-1", "")] = state
        except json.JSONDecodeError:
            continue

    down = [s for s in SERVICES if running.get(s) != "running"]
    if not down:
        return {"status": "up", "detail": f"all {len(SERVICES)} containers running"}

    # Try to start missing containers
    try:
        up_proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", *down,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=60,
        )
        _, up_stderr = await up_proc.communicate()
        if up_proc.returncode != 0:
            return {"status": "degraded", "detail": f"restart {down} failed: {up_stderr.decode('utf-8', errors='replace').strip()[:200]}"}
        return {"status": "up", "detail": f"restarted: {', '.join(down)}"}
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        return {"status": "degraded", "detail": f"docker compose up failed: {e}"}
