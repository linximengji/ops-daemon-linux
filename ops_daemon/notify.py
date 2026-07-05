"""Notification dispatch — feishu API + terminal fallback."""
import json, os, sys
import httpx

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID", "ou_6a6b52dc63d4051834ae522a3a6e7775")

TENANT_TOKEN: str | None = None


async def _get_tenant_token() -> str | None:
    global TENANT_TOKEN
    if TENANT_TOKEN:
        return TENANT_TOKEN
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={
                "app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET,
            })
            TENANT_TOKEN = r.json().get("tenant_access_token")
            return TENANT_TOKEN
    except Exception:
        return None


async def notify_feishu(severity: str, title: str, message: str, receive_id: str | None = None) -> bool:
    """Send alert to feishu via tenant API. Returns True on success."""
    token = await _get_tenant_token()
    if not token:
        return False
    target = receive_id or RECEIVE_ID
    color_map = {"INFO": "blue", "WARN": "orange", "CRITICAL": "red"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": target,
                    "msg_type": "interactive",
                    "content": json.dumps({
                        "config": {"wide_screen_mode": True},
                        "header": {"title": {"tag": "plain_text", "content": f"[{severity}] {title}"},
                                   "template": color_map.get(severity, "blue")},
                        "elements": [{"tag": "markdown", "content": message}],
                    }),
                },
            )
            return r.status_code == 200
    except Exception:
        return False


def notify_terminal(severity: str, title: str, message: str):
    """Fallback: print to stderr."""
    line = f"[{severity}] {title}: {message}"
    print(line, file=sys.stderr)


async def notify(severity: str, title: str, message: str, receive_id: str | None = None):
    """Dispatch to feishu, fallback to terminal. Optional per-call receive_id override."""
    if receive_id:
        ok = await notify_feishu(severity, title, message, receive_id)
    else:
        ok = await notify_feishu(severity, title, message)
    if not ok:
        notify_terminal(severity, title, message)
