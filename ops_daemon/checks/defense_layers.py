"""defense_layers - aggregate L1 (Pact Broker), L2 (Jaeger), L3 (Git diff) into
a single check result for the Pact page."""
import asyncio
import httpx
import json
import os
import subprocess
from pathlib import Path

PACT_BROKER_URL = "http://localhost:9292"
JAEGER_QUERY_URL = "http://localhost:16686"
PROJECTS_ROOT = "/home/ubuntu/projects"
EXCLUDE_PROJECTS = {"awesome-selfhosted", "presenton-src"}


async def _tcp_probe(host: str, port: int, timeout: int = 3) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


async def _check_l1_pact(cfg: dict) -> dict:
    url = cfg.get("pact_broker_url", PACT_BROKER_URL)
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 9292)
    timeout = cfg.get("timeout_seconds", 5)

    result: dict = {"status": "down", "broker": url, "contracts": []}
    if not await _tcp_probe(host, port, timeout):
        return result

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        try:
            r = await asyncio.wait_for(client.get(f"{url}/pacts/latest"), timeout=timeout)
            pacts_data = r.json()
        except Exception:
            result["status"] = "degraded"
            return result

        pacts = pacts_data.get("pacts", [])
        contracts = []
        for p in pacts:
            emb = p.get("_embedded", {})
            consumer = emb.get("consumer", {})
            provider = emb.get("provider", {})
            cname = consumer.get("name", "?")
            pname = provider.get("name", "?")
            version_emb = consumer.get("_embedded", {}).get("version", {})
            ver_number = version_emb.get("number", "?")
            verification = None
            self_link = p.get("_links", {}).get("self")
            if isinstance(self_link, list) and self_link:
                detail_href = self_link[0].get("href")
            else:
                detail_href = None
            if detail_href:
                try:
                    r2 = await asyncio.wait_for(client.get(detail_href), timeout=timeout)
                    detail_data = r2.json()
                except Exception:
                    detail_data = None
                if detail_data:
                    vr_link = detail_data.get("_links", {}).get("pb:latest-verification-results", {})
                    vr_href = vr_link.get("href") if isinstance(vr_link, dict) else None
                    if vr_href:
                        try:
                            r3 = await asyncio.wait_for(client.get(vr_href), timeout=timeout)
                            vr_data = r3.json()
                        except Exception:
                            vr_data = None
                        if vr_data and isinstance(vr_data, dict) and "success" in vr_data:
                            verification = {
                                "success": vr_data["success"],
                                "testResults": vr_data.get("testResults"),
                                "verifiedAt": vr_data.get("verificationDate"),
                            }

            contracts.append({
                "consumer": cname,
                "provider": pname,
                "version": ver_number,
                "createdAt": p.get("createdAt"),
                "verification": verification,
            })

        result["status"] = "up"
        result["contracts"] = contracts
        return result


async def _check_l2_jaeger(cfg: dict) -> dict:
    result: dict = {"status": "down", "ports": {"otlp": False, "ui": False}, "services": []}

    otlp_up = await _tcp_probe("127.0.0.1", 4317, 3)
    ui_up = await _tcp_probe("127.0.0.1", 16686, 3)
    result["ports"]["otlp"] = otlp_up
    result["ports"]["ui"] = ui_up

    if not ui_up:
        result["status"] = "down"
        return result

    async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
        try:
            r = await asyncio.wait_for(client.get(f"{JAEGER_QUERY_URL}/api/services"), timeout=5)
            svc_data = r.json()
        except Exception:
            svc_data = None

        if svc_data:
            result["services"] = svc_data.get("data", [])
            result["status"] = "up"
        else:
            result["status"] = "degraded"

    return result


async def _git_status_porcelain(path: str) -> int:
    try:
        r = await asyncio.create_subprocess_exec(
            "git", "-C", path, "status", "--porcelain",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
        return len([line for line in out.decode("utf-8", errors="replace").splitlines() if line.strip()])
    except (subprocess.TimeoutExpired, asyncio.TimeoutError):
        return -1


async def _git_unpushed(path: str) -> int:
    try:
        r = await asyncio.create_subprocess_exec(
            "git", "-C", path, "log", "@{u}..", "--oneline",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(r.communicate(), timeout=5)
        err_str = err.decode("utf-8", errors="replace").strip()
        if "no upstream" in err_str.lower() or r.returncode != 0:
            return 0
        return len([line for line in out.decode("utf-8", errors="replace").splitlines() if line.strip()])
    except (subprocess.TimeoutExpired, asyncio.TimeoutError):
        return -1


async def _git_last_commit(path: str) -> str:
    try:
        r = await asyncio.create_subprocess_exec(
            "git", "-C", path, "log", "-1", "--format=%ci",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
        return out.decode("utf-8", errors="replace").strip()[:10]
    except (subprocess.TimeoutExpired, asyncio.TimeoutError):
        return "?"


async def _check_l3_project(entry: str, proj_path: str) -> dict:
    uncommitted, unpushed, last_commit = await asyncio.gather(
        _git_status_porcelain(proj_path),
        _git_unpushed(proj_path),
        _git_last_commit(proj_path),
    )
    return {
        "name": entry,
        "uncommitted": uncommitted,
        "unpushed": unpushed,
        "last_commit": last_commit,
    }


async def _check_l3_git(cfg: dict) -> dict:
    root = cfg.get("projects_root", PROJECTS_ROOT)
    exclude = set(cfg.get("exclude_projects", EXCLUDE_PROJECTS))
    proj_root = Path(root)

    if not proj_root.exists():
        return {"status": "down", "projects": [], "error": f"{root} not found"}

    git_projects = [
        (entry, str(proj_root / entry))
        for entry in sorted(os.listdir(proj_root))
        if entry not in exclude and (proj_root / entry / ".git").exists()
    ]
    projects = await asyncio.gather(
        *(_check_l3_project(entry, path) for entry, path in git_projects))

    dirty = [p for p in projects if p["uncommitted"] > 0 or p["unpushed"] > 0]
    status = "warn" if dirty else "ok"
    return {"status": status, "projects": projects}


async def check_defense_layers(cfg: dict) -> dict:
    l1_cfg = cfg.get("l1", {})
    l2_cfg = cfg.get("l2", {})
    l3_cfg = cfg.get("l3", {})
    l1, l2, l3 = await asyncio.gather(
        _check_l1_pact(l1_cfg),
        _check_l2_jaeger(l2_cfg),
        _check_l3_git(l3_cfg),
    )

    statuses = [l1["status"], l2["status"], l3["status"]]
    if any(s == "down" for s in statuses):
        overall = "critical"
    elif any(s == "degraded" or s == "warn" for s in statuses):
        overall = "warn"
    elif any(s == "unknown" for s in statuses):
        overall = "warn"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "layers": {
            "layer1_contract": l1,
            "layer2_tracing": l2,
            "layer3_diff": l3,
        },
    }
