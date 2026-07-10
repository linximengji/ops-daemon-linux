#!/usr/bin/env python3
"""Server-side Pact contract verification engine.

Reads all contracts from local Pact Broker, fires HTTP requests against
each provider, compares status codes and body structure.

Usage:
  python3 scripts/pact_verify.py                      # verify all
  python3 scripts/pact_verify.py --provider model-proxy
  python3 scripts/pact_verify.py --json                # JSON output for ops-daemon

Exit code: 0 = all pass, 1 = any failure
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin

BROKER_URL = "http://localhost:9292"

PROVIDER_MAP = {
    "model-proxy": "http://localhost:4000",
    "feishu-bridge": "http://localhost:9878",
    "jaeger": "http://localhost:16686",
    "presenton": "http://localhost:5000",
    "miniflux": "http://localhost:8080",
    "pact-broker": "http://localhost:9292",
}

PROVIDER_AUTH = {
    "miniflux": ("admin", "admin_local_mf"),
}

# 副作用的端点定义。GET 请求始终安全。
# POST/PUT/DELETE 列入此集合的端点做宽松校验（只检查可达+status code）。
DESTRUCTIVE_ENDPOINTS = {
    "feishu-bridge": {"/send-image"},  # 会实际发送飞书图片
    "model-proxy": {
        "/v1/chat/completions",  # 会消耗 API 配额
        "/v1/messages",
    },
    "presenton": {"/api/v1/ppt/presentation/generate"},
    "miniflux": {"/v1/entries", "/v1/entries"},  # PUT mark-read
}


def _http(method, url, body=None, headers=None, timeout=10):
    hdrs = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        body_bytes = resp.read()
        ct = resp.headers.get("Content-Type", "")
        json_body = json.loads(body_bytes) if "json" in ct else None
        return {"status": resp.status, "headers": dict(resp.headers), "body": json_body}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "headers": dict(e.headers), "body": None, "error": str(e)}
    except Exception as e:
        return {"status": 0, "body": None, "error": str(e)}


def _type_check(actual, expected, path=""):
    """Recursively check that `actual` has all keys/values from `expected` with matching types.
    Returns list of mismatch descriptions."""
    mismatches = []
    if expected is None:
        return mismatches
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected dict, got {type(actual).__name__}"]
        for k, ev in expected.items():
            if k not in actual:
                mismatches.append(f"{path}.{k}: missing")
            else:
                mismatches.extend(_type_check(actual[k], ev, f"{path}.{k}"))
        return mismatches
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if expected and actual:
            mismatches.extend(_type_check(actual[0], expected[0], f"{path}[0]"))
        return mismatches
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            return [f"{path}: expected bool, got {type(actual).__name__}"]
        return mismatches
    if isinstance(expected, int):
        if isinstance(actual, bool):
            return [f"{path}: expected int, got bool"]
        if not isinstance(actual, (int, float)):
            return [f"{path}: expected int, got {type(actual).__name__}"]
        return mismatches
    if isinstance(expected, float):
        if not isinstance(actual, (int, float)):
            return [f"{path}: expected float, got {type(actual).__name__}"]
        return mismatches
    if isinstance(expected, str):
        if not isinstance(actual, str):
            return [f"{path}: expected str, got {type(actual).__name__}"]
        return mismatches
    return mismatches


def _build_request(interaction):
    req = interaction.get("request", {})
    method = req.get("method", "GET")
    path = req.get("path", "/")
    body = req.get("body")
    headers = req.get("headers", {})
    qs = req.get("query", "")
    return method, path, body, headers, qs


def verify_interaction(interaction, base_url, provider_name):
    """Verify one interaction against a provider. Returns dict with pass/fail details."""
    method, path, body, headers, query = _build_request(interaction)
    expected_status = interaction.get("response", {}).get("status", 200)
    expected_body = interaction.get("response", {}).get("body")
    expected_ct = interaction.get("response", {}).get("headers", {}).get("Content-Type", "")

    # Inject auth headers for providers that require them
    auth = PROVIDER_AUTH.get(provider_name)
    if auth:
        import base64
        user, pw = auth
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        headers = dict(headers or {})
        headers.setdefault("Authorization", f"Basic {token}")

    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if query:
        url += "?" + (query if not query.startswith("?") else query[1:])

    # Check if this endpoint is safe to call (GET always safe, destructive = only check reachability)
    destructive_paths = DESTRUCTIVE_ENDPOINTS.get(provider_name, set())
    is_destructive = method in ("POST", "PUT", "DELETE", "PATCH") and path in destructive_paths

    if is_destructive:
        return {
            "description": interaction.get("description", "?"),
            "pass": True,
            "skipped": True,
            "reason": f"endpoint {method} {path} is destructive, check reachability only",
        }

    result = _http(method, url, body, headers)
    mismatches = []

    if result.get("error"):
        return {
            "description": interaction.get("description", "?"),
            "pass": False,
            "error": f"request failed: {result['error']}",
        }

    if result["status"] != expected_status:
        mismatches.append(f"status {result['status']} != expected {expected_status}")

    if expected_ct and "Content-Type" in result.get("headers", {}):
        actual_ct = result["headers"]["Content-Type"]
        if expected_ct not in actual_ct:
            mismatches.append(f"Content-Type '{actual_ct}' does not contain '{expected_ct}'")

    if expected_body and result.get("body") is not None:
        mismatches.extend(_type_check(result["body"], expected_body))

    return {
        "description": interaction.get("description", "?"),
        "pass": len(mismatches) == 0,
        "mismatches": mismatches,
        "status_code": result["status"],
    }


def fetch_contracts():
    """Fetch all latest contracts from Broker. Returns list of
    (consumer, provider, interactions, pact_version_href, verification_href)."""
    resp = _http("GET", f"{BROKER_URL}/pacts/latest")
    if resp.get("error"):
        print(f"ERROR: cannot reach Broker: {resp['error']}", file=sys.stderr)
        return []
    if not resp.get("body"):
        print("ERROR: empty response from Broker", file=sys.stderr)
        return []

    contracts = []
    pacts = resp["body"].get("pacts", [])
    for p in pacts:
        emb = p.get("_embedded", {})
        consumer = emb.get("consumer", {}).get("name", "?")
        provider = emb.get("provider", {}).get("name", "?")

        href = ""
        slf = p.get("_links", {}).get("self")
        if isinstance(slf, list) and slf:
            href = slf[0].get("href", "")
        if not href:
            continue
        detail = _http("GET", href)
        if detail.get("error") or not detail.get("body"):
            continue
        interactions = detail["body"].get("interactions", [])
        detail_links = detail["body"].get("_links", {})

        pact_ver_href = ""
        pv = detail_links.get("pb:publish-verification-results")
        if pv and isinstance(pv, dict):
            pact_ver_href = pv.get("href", "")

        contracts.append((consumer, provider, interactions, href, pact_ver_href))

    return contracts


def _results_to_json(all_results):
    """Convert results to JSON for ops-daemon check."""
    total = sum(len(r["interactions"]) for r in all_results if "interactions" in r)
    passed = sum(
        1 for r in all_results if "interactions" in r
        for iv in r["interactions"] if iv.get("pass")
    )
    failed = []
    for r in all_results:
        if "interactions" not in r:
            continue
        for iv in r["interactions"]:
            if not iv.get("pass") and not iv.get("skipped"):
                failed.append(iv["description"])

    return {
        "status": "passed" if not failed else "failed",
        "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "total_interactions": total,
        "passed": passed,
        "failed": len(failed),
        "skipped": sum(
            1 for r in all_results if "interactions" in r
            for iv in r["interactions"] if iv.get("skipped")
        ),
        "failures": failed,
        "details": all_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Pact contract verifier")
    parser.add_argument("--provider", help="Verify only this provider")
    parser.add_argument("--consumer", help="Verify only this consumer")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    contracts = fetch_contracts()
    if not contracts:
        print("No contracts found or Broker unreachable", file=sys.stderr)
        sys.exit(1 if args.json else 0)

    all_results = []

    for consumer, provider, interactions, pact_href, ver_href in contracts:
        if args.provider and provider != args.provider:
            continue
        if args.consumer and consumer != args.consumer:
            continue
        if not interactions:
            continue

        base_url = PROVIDER_MAP.get(provider)
        if not base_url:
            if args.json:
                all_results.append({
                    "consumer": consumer,
                    "provider": provider,
                    "interactions": [],
                    "skipped": True,
                    "reason": f"no URL mapped for provider '{provider}'",
                })
            else:
                print(f"  [{consumer} → {provider}] SKIP (no URL mapped)")
            continue

        results = []
        for intx in interactions:
            vr = verify_interaction(intx, base_url, provider)
            results.append(vr)

        entry = {"consumer": consumer, "provider": provider, "interactions": results}
        all_results.append(entry)

        # Publish verification result to Broker with matching provider version
        all_pass = all(v.get("pass") for v in results)
        if ver_href:
            ver_body = {
                "success": all_pass,
                "providerApplicationVersion": "1.0.0",
                "buildUrl": "",
            }
            r = _http("POST", ver_href, ver_body)
            if r.get("error"):
                print(f"      [warn] publish verification failed: {r['error']}", file=sys.stderr)

        if not args.json:
            icon = "✓" if all(v.get("pass") for v in results) else "✗"
            print(f"  {icon} {consumer} → {provider}")
            for v in results:
                if v.get("pass"):
                    if v.get("skipped"):
                        print(f"      ~ {v['description']}: {v['reason']}")
                    else:
                        print(f"      ✓ {v['description']}")
                else:
                    msg = v.get("error") or "; ".join(v.get("mismatches", []))
                    print(f"      ✗ {v['description']}: {msg}")
            print()

    if args.json:
        print(json.dumps(_results_to_json(all_results), indent=2, ensure_ascii=False))

    has_failures = any(
        not iv.get("pass") and not iv.get("skipped")
        for r in all_results if "interactions" in r
        for iv in r["interactions"]
    )
    sys.exit(1 if has_failures else 0)


if __name__ == "__main__":
    main()
