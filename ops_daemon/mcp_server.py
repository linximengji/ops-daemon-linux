"""Standalone MCP server for CC to query ops-daemon state and manage tasks.
Run alongside ops-daemon: reads/writes shared data/ directory."""
import sys, json, time, re
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

from agent_core.mcp import build_mcp
from agent_core import StateStore
from agent_core.scheduler import CronExpr


TASKS_PATH = Path(__file__).parent.parent / "data" / "tasks.json"
PHONE_TASKS_INDEX = Path(__file__).parent.parent.parent / "tasks" / "index.json"


def _read_tasks() -> list[dict]:
    if not TASKS_PATH.exists():
        return []
    return json.loads(TASKS_PATH.read_text())


def _write_tasks(tasks: list[dict]):
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(json.dumps(tasks, indent=2))


def main():
    data_dir = Path(__file__).parent.parent / "data"
    store = StateStore(str(data_dir))
    mcp = build_mcp("ops-daemon", store)

    @mcp.tool()
    async def list_tasks() -> list[dict]:
        """Return all registered scheduled tasks with next-run estimate."""
        now = time.time()
        tasks = _read_tasks()
        out = []
        for t in tasks:
            name = t["name"]
            sched = t["schedule"]
            last = t.get("last_run", 0)
            entry = {"name": name, "schedule": sched, "last_run": last}
            if sched.startswith("interval:"):
                secs = int(sched.split(":", 1)[1])
                entry["next_run"] = last + secs if last else now
            else:
                try:
                    entry["next_run"] = CronExpr(sched).next_after(now)
                except (ValueError, IndexError):
                    entry["next_run"] = 0
            out.append(entry)
        return out

    @mcp.tool()
    async def add_task(name: str, schedule: str) -> dict:
        """Register a new scheduled task. schedule is a cron expr or 'interval:N'."""
        tasks = [t for t in _read_tasks() if t["name"] != name]
        tasks.append({"name": name, "schedule": schedule, "last_run": 0})
        _write_tasks(tasks)
        return {"status": "ok", "name": name, "schedule": schedule}

    @mcp.tool()
    async def remove_task(name: str) -> dict:
        """Unregister a scheduled task by name."""
        tasks = _read_tasks()
        before = len(tasks)
        tasks = [t for t in tasks if t["name"] != name]
        _write_tasks(tasks)
        return {"status": "ok", "removed": len(tasks) != before}

    # ── tasks index tools ──

    def _read_phone_index() -> dict:
        if not PHONE_TASKS_INDEX.exists():
            return {}
        return json.loads(PHONE_TASKS_INDEX.read_text(encoding="utf-8"))

    def _write_phone_index(index: dict):
        PHONE_TASKS_INDEX.parent.mkdir(parents=True, exist_ok=True)
        PHONE_TASKS_INDEX.write_text(
            json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    @mcp.tool()
    async def list_phone_tasks(
        status: str = "", limit: int = 20
    ) -> list[dict]:
        """List personal phone tasks with optional status filter ('pending'/'completed')."""
        index = _read_phone_index()
        entries = []
        for task_id, val in index.items():
            if status and val.get("status") != status:
                continue
            entries.append({
                "id": task_id,
                "summary": val.get("summary", ""),
                "status": val.get("status", "unknown"),
                "created_at": val.get("created_at", ""),
                "updated_at": val.get("updated_at"),
            })
        entries.sort(key=lambda e: e["id"], reverse=True)
        return entries[:limit]

    @mcp.tool()
    async def add_phone_task(summary: str) -> dict:
        """Add a new pending personal task manually."""
        if not summary.strip():
            return {"status": "error", "message": "summary is required"}
        today = datetime.now().strftime("%Y-%m-%d")
        slug = re.sub(r"[^一-龥a-zA-Z0-9]", "-", summary.strip())[:40].strip("-") or "task"
        index = _read_phone_index()
        # find next seq for today
        seq = 1
        prefix = f"{today}/"
        for task_id in index:
            if task_id.startswith(prefix):
                parts = task_id[len(prefix) :].split("-", 1)
                if parts and parts[0].isdigit():
                    seq = max(seq, int(parts[0]) + 1)
        task_id = f"{today}/{str(seq).zfill(3)}-{slug}"
        now = datetime.now().isoformat()
        index[task_id] = {
            "status": "pending",
            "summary": summary.strip()[:80],
            "created_at": now,
            "updated_at": None,
            "type": "task",
            "source": "ops-daemon",
        }
        _write_phone_index(index)
        return {"status": "ok", "id": task_id, "summary": summary.strip()[:80]}

    @mcp.tool()
    async def close_phone_task(task_id: str) -> dict:
        """Mark a pending phone task as completed by its task_id."""
        index = _read_phone_index()
        if task_id not in index:
            return {"status": "error", "message": f"task not found: {task_id}"}
        index[task_id]["status"] = "completed"
        index[task_id]["updated_at"] = datetime.now().isoformat()
        _write_phone_index(index)
        return {
            "status": "ok",
            "id": task_id,
            "summary": index[task_id].get("summary", ""),
        }

    @mcp.tool()
    async def delete_phone_task(task_id: str) -> dict:
        """Permanently remove a phone task entry from the index."""
        index = _read_phone_index()
        if task_id not in index:
            return {"status": "error", "message": f"task not found: {task_id}"}
        summary = index[task_id].get("summary", "")
        del index[task_id]
        _write_phone_index(index)
        return {"status": "ok", "id": task_id, "summary": summary}

    # ── diagnosis tool ──

    @mcp.tool()
    async def diagnose(event_type: str = "", hours: int = 24) -> str:
        """Run LLM diagnosis on recent events. event_type filters (optional)."""
        recent = store.load_episodic(days=max(1, hours // 24 + 1))
        if event_type:
            recent = [e for e in recent if e.get("type") == event_type]
        from ops_daemon.llm import diagnose as _d
        return await _d(event_type or "manual", {}, recent)

    mcp.run()


if __name__ == "__main__":
    main()
