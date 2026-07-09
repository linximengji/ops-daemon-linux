import hashlib
import json
import os
from datetime import datetime
from pathlib import Path


MANIFEST_PATH = Path(__file__).parent.parent.parent / "data" / "file_integrity_manifest.json"

_HOME = Path.home()
_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS", str(_HOME / "projects")))
DEFAULT_PATHS = [
    str(_HOME / ".claude" / "hooks" / "protect_services.py"),
    str(_HOME / ".claude" / "hooks" / "bash_length_guard.py"),
    str(_HOME / ".claude" / "hooks" / "pre_tool_guard.py"),
    str(_HOME / ".claude" / "hooks" / "bash_metachar_filter.py"),
    str(_HOME / ".claude" / "hooks" / "post_failure_recorder.py"),
    str(_HOME / ".claude" / "hooks" / "audit_logger.py"),
    str(_HOME / ".claude" / "settings.json"),
    str(_PROJECTS / "ops-daemon" / "ops_daemon" / "process_manager.py"),
    str(_PROJECTS / "ops-daemon" / "ops_daemon" / "repair.py"),
    str(_PROJECTS / "ops-daemon" / "ops_daemon" / "state_machine.py"),
]


def _sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (FileNotFoundError, PermissionError, OSError):
        return None


async def check_file_integrity(cfg: dict, store) -> dict:
    paths = cfg.get("paths", DEFAULT_PATHS)
    seq = cfg.get("startup_seq", 3)

    current = {}
    for p in paths:
        h = _sha256(p)
        if h:
            current[p] = h

    if not MANIFEST_PATH.exists():
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps({
            "created_at": datetime.now().isoformat(),
            "hashes": current,
            "startup_seq": 0,
        }, indent=2))
        return {"status": "initialized", "files": len(current)}

    manifest = json.loads(MANIFEST_PATH.read_text())
    stored = manifest.get("hashes", {})
    startup_count = manifest.get("startup_seq", 0)

    if startup_count < seq:
        manifest["startup_seq"] = startup_count + 1
        manifest["hashes"] = current
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
        return {"status": "baseline", "files": len(current), "seq": startup_count + 1}

    tampered = []
    missing = []
    for p in paths:
        new_hash = current.get(p)
        old_hash = stored.get(p)
        if new_hash is None:
            if old_hash is not None:
                missing.append(p)
        elif old_hash is None:
            tampered.append({"path": p, "reason": "new_file", "hash": new_hash})
        elif new_hash != old_hash:
            tampered.append({"path": p, "reason": "modified", "old": old_hash, "new": new_hash})

    if tampered or missing:
        store.append_episodic({
            "type": "file_integrity_alert",
            "tampered": tampered,
            "missing": missing,
        })
        return {
            "status": "alert",
            "tampered_count": len(tampered),
            "missing_count": len(missing),
            "tampered": tampered,
            "missing": missing,
        }

    return {"status": "ok", "files": len(current)}
