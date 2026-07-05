"""LLM integration — anomaly diagnosis via DeepSeek API (bypass proxy)."""
import os, json, time
import httpx

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com") + "/v1/chat/completions"


async def diagnose(event_type: str, context: dict, recent_events: list[dict]) -> str:
    """Analyze anomaly with LLM and return diagnosis text."""
    if not API_KEY:
        return "LLM diagnosis unavailable: DEEPSEEK_API_KEY not set"

    events_text = "\n".join(
        f"  [{e.get('ts','?')}] {e.get('type','?')}: {json.dumps({k:v for k,v in e.items() if k not in ('ts',)})}"
        for e in recent_events[-20:]
    )

    prompt = f"""你是一个运维诊断助手。分析以下异常事件和相关上下文，给出：
1. 根因推测
2. 建议行动
3. 严重程度判断

异常类型: {event_type}
异常上下文: {json.dumps(context, ensure_ascii=False)}
最近事件:
{events_text}
"""

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(API_URL, json=payload, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            return text.strip()
    except Exception as e:
        return f"LLM diagnosis failed: {e}"
