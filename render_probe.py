"""
Render Deployment Verification Probe
=====================================
Run after deploying to Render to confirm the live API is healthy.

Usage:
    python render_probe.py <RENDER_URL> <API_TOKEN>

Example:
    python render_probe.py https://cat-translate-api.onrender.com cat-translate-dev-token-secret-123456789
"""
from __future__ import annotations

import sys
import time
import json
import urllib.request
import urllib.error


def http(method, url, token="", body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def sep(title):
    print(f"\n{'=' * 56}")
    print(f"  {title}")
    print("=" * 56)


def chk(label, passed, detail=""):
    icon = "PASS" if passed else "FAIL"
    extra = f"  ({detail})" if detail else ""
    print(f"  [{icon}]  {label}{extra}")
    return passed


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    base = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    results = []

    # Step 1: Health (unauthenticated)
    sep("STEP 1 — Health Check  (no token required)")
    t0 = time.perf_counter()
    status, body = http("GET", f"{base}/api/health")
    ms = int((time.perf_counter() - t0) * 1000)
    ok = status == 200 and body.get("ok") is True
    results.append(chk("GET /api/health  -> 200", ok, f"{ms} ms"))
    results.append(chk("body.ok == true", body.get("ok") is True))
    if body:
        print(f"     version : {body.get('version', '?')}")

    # Step 2: Auth rejection
    sep("STEP 2 — Auth Guard  (wrong token must get 401)")
    status, _ = http("GET", f"{base}/api/engines", token="totally-wrong-token")
    results.append(chk("Bad token -> 401", status == 401, f"HTTP {status}"))

    # Step 3: Engine snapshot
    sep("STEP 3 — Engine Snapshot  (authenticated)")
    t0 = time.perf_counter()
    status, body = http("GET", f"{base}/api/engines", token=token)
    ms = int((time.perf_counter() - t0) * 1000)
    ok = status == 200 and "engines" in body
    results.append(chk("GET /api/engines -> 200", ok, f"{ms} ms"))
    if ok:
        for eng in body.get("engines", []):
            print(f"     {eng['name']:15s}  status={eng['status']}  pos={eng.get('position_in_default_chain', '?')}")
        chain = body.get("default_chain", [])
        results.append(chk("default_chain starts with 'gemini'", chain and chain[0] == "gemini", str(chain)))

    # Step 4: Live translation
    sep("STEP 4 — Live Translation  (en -> bn, digits + tag)")
    payload = {
        "sl": "en",
        "tl": "bn",
        "segments": [
            "Hello, how are you today?",
            "Please check document 102 {0} before submitting.",
        ],
    }
    t0 = time.perf_counter()
    status, body = http("POST", f"{base}/api/translate", token=token, body=payload)
    ms = int((time.perf_counter() - t0) * 1000)
    ok = status == 200
    results.append(chk("POST /api/translate -> 200", ok, f"{ms} ms"))
    if ok:
        segs = body.get("results", [])
        for i, r in enumerate(segs):
            has_text = r.get("text") is not None
            via = r.get("via", "?")
            results.append(chk(f"segment {i+1} has text", has_text, f"via={via}"))
            if has_text:
                digits_ok = "102" in r["text"]
                results.append(chk(f"segment {i+1} ASCII digits preserved (102)", digits_ok, r["text"][:55]))
            if i == 1 and has_text:
                tag_ok = "{0}" in r["text"]
                results.append(chk("segment 2 tag {0} preserved", tag_ok, r["text"][:55]))
        meta = body.get("meta", {})
        print(f"\n     elapsed_ms   : {meta.get('elapsed_ms')}")
        print(f"     queue_wait   : {meta.get('queue_wait_ms')} ms")
        print(f"     engine_chain : {meta.get('engines')}")

    # Step 5: Input validation
    sep("STEP 5 — Input Validation  (empty segments -> 400)")
    status, _ = http("POST", f"{base}/api/translate", token=token,
                     body={"sl": "en", "tl": "bn", "segments": []})
    results.append(chk("Empty segments -> 400", status == 400, f"HTTP {status}"))

    # Verdict
    sep("FINAL VERDICT")
    failed = results.count(False)
    passed = results.count(True)
    if failed == 0:
        print(f"  ALL {passed} checks PASSED -- Render deployment is healthy!\n")
    else:
        print(f"  {failed} check(s) FAILED out of {passed + failed}. Review output above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
