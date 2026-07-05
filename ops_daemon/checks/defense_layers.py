"""3-Layer defense status: L1 contract / L2 distributed tracing / L3 git diff.

Enhanced to pull full contract details from Pact Broker API (like the old pact-site),
Jaeger services, and scan all git projects under D:/ClaudeProjects/.
"""
import asyncio, json, os, socket, subprocess, urllib.request, urllib.error
from pathlib import Path

BROKER_URL = os.environ.get("PACT_BROKER_URL", "http://localhost:9292")
JAEGER_API = "http://localhost:16686"
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS", str(Path.home() / "projects")))


async def _fetch(url: str, timeout: float = 5.0) -> dict | None:
    """Async HTTP GET, returns parsed JSON or None."""
    try:
        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: urllib.request.urlopen(url, timeout=timeout)),
            timeout=timeout + 1.0,
        )
        return json.loads(resp.read().decode())
    except Exception:
        return None


def _check_port(port: int, timeout: float = 2.0) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


async def check_defense_layers(cfg: dict, store=None) -> dict:
    results = {}

    # ── L1: Contract Verification ──────────────────────────────────────────
    broker_ok = bool(await _fetch(f"{BROKER_URL}/"))
    contracts: list[dict] = []

    if broker_ok:
        pacts_data = await _fetch(f"{BROKER_URL}/pacts/latest")
        pact_links: list[dict] = (
            (pacts_data or {}).get("_links", {}).get("pb:pacts", [])
            or (pacts_data or {}).get("_links", {}).get("pacts", [])
        )

        async def _fetch_pact_detail(pl: dict) -> dict | None:
            href = pl.get("href")
            if not href:
                return None
            pact = await _fetch(href)
            if not pact:
                return None
            consumer = (pact.get("consumer") or {}).get("name", "?")
            provider = (pact.get("provider") or {}).get("name", "?")
            version = (
                (pact.get("_links") or {}).get("pb:pact-version", {}).get("name")
                or (pact.get("createdAt") or "?")[:12]
            )
            # Fetch verification results
            v_link = (pact.get("_links") or {}).get("pb:latest-verification-results", {}).get("href")
            verification = None
            if v_link:
                v_data = await _fetch(v_link)
                if v_data:
                    verification = {
                        "success": v_data.get("success"),
                        "verifiedAt": v_data.get("verificationDate") or v_data.get("createdAt"),
                        "verifier": v_data.get("verifier"),
                        "testResults": {
                            "total": (v_data.get("testResults") or {}).get("total"),
                            "passed": (v_data.get("testResults") or {}).get("passed"),
                            "failed": (v_data.get("testResults") or {}).get("failed"),
                        },
                    }
            return {"consumer": consumer, "provider": provider, "version": version, "verification": verification}

        if pact_links:
            batch = await asyncio.gather(*[_fetch_pact_detail(pl) for pl in pact_links], return_exceptions=True)
            contracts = [c for c in batch if c and not isinstance(c, BaseException)]

    # Compute status: if any contract failed → degraded
    failed_contracts = sum(1 for c in contracts if c.get("verification") and not c["verification"]["success"])
    l1_status = "down" if not broker_ok else ("degraded" if failed_contracts > 0 else "up")

    # Read verify.py detail from store
    verify_detail = "not checked yet"
    if store:
        working = store.load_working()
        pv = working.get("pact_verify", {})
        verify_detail = pv.get("detail") or pv.get("error") or verify_detail

    results["layer1_contract"] = {
        "status": l1_status,
        "detail": (
            f"Pact Broker: {'up' if broker_ok else 'down'}, "
            f"{len(contracts)} contracts, {failed_contracts} failed"
        ),
        "broker": "up" if broker_ok else "down",
        "contracts": contracts,
        "verify": verify_detail,
    }

    # ── L2: Distributed Tracing (Jaeger/OTel) ──────────────────────────────
    otlp_up = _check_port(4317)
    ui_up = _check_port(16686)

    svc_data = await _fetch(f"{JAEGER_API}/api/services") if ui_up else None
    services: list[str] = svc_data.get("data", []) if svc_data else []

    open_ports = sum([otlp_up, ui_up])
    l2_status = "up" if open_ports == 2 else ("degraded" if open_ports > 0 else "down")

    results["layer2_tracing"] = {
        "status": l2_status,
        "detail": f"Jaeger: {open_ports}/2 ports open (4317 OTLP, 16686 UI), {len(services)} services",
        "ports": {"otlp": otlp_up, "ui": ui_up},
        "services": services,
    }

    # ── L3: Git Diff / Code Review ─────────────────────────────────────────
    projects: list[dict] = []
    if CLAUDE_PROJECTS.is_dir():
        for entry in sorted(CLAUDE_PROJECTS.iterdir()):
            if not entry.is_dir():
                continue
            git_dir = entry / ".git"
            if not git_dir.is_dir():
                continue
            try:
                r1 = subprocess.run(
                    ["git", "-C", str(entry), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                uncommitted = len([l for l in r1.stdout.split("\n") if l.strip()])
                r2 = subprocess.run(
                    ["git", "-C", str(entry), "log", "--oneline", "@{u}..HEAD", "--"],
                    capture_output=True, text=True, timeout=5,
                )
                unpushed = len([l for l in r2.stdout.split("\n") if l.strip()])
                r3 = subprocess.run(
                    ["git", "-C", str(entry), "log", "-1", "--format=%ci"],
                    capture_output=True, text=True, timeout=5,
                )
                last_commit = r3.stdout.strip()
                proj: dict = {"name": entry.name, "last_commit": last_commit}
                if uncommitted > 0 or unpushed > 0:
                    proj["uncommitted"] = uncommitted
                    proj["unpushed"] = unpushed
                projects.append(proj)
            except (subprocess.TimeoutExpired, OSError):
                pass  # skip repos that fail

    dirty = [p for p in projects if p.get("uncommitted") or p.get("unpushed")]
    clean = [p for p in projects if not p.get("uncommitted") and not p.get("unpushed")]
    parts = []
    for p in dirty:
        parts.append(f"{p['name']}: {p['uncommitted']} uncommitted, {p['unpushed']} unpushed (last: {p['last_commit'][:10]})")
    for p in clean:
        parts.append(f"{p['name']}: clean (last: {p['last_commit'][:10]})")
    diff_detail = "; ".join(parts) if parts else "clean"
    l3_status = "up" if not dirty else "degraded"

    results["layer3_diff"] = {
        "status": l3_status,
        "detail": diff_detail,
        "projects": projects,
    }

    # ── Aggregate ──────────────────────────────────────────────────────────
    status_weights = {"up": 1, "degraded": 0.5, "down": 0}
    total_score = sum(status_weights.get(v.get("status", "down"), 0) for v in results.values())
    if total_score >= 2.5:
        aggregated = "up"
    elif total_score >= 1:
        aggregated = "degraded"
    else:
        aggregated = "down"

    return {
        "status": aggregated,
        "detail": f"{total_score:.0f}/3 layers healthy",
        "layers": results,
    }
