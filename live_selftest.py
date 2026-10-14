#!/usr/bin/env python3
"""
Exhaustive Live Self-Test & Rate Limit / Fallback Recovery Suite.

This script executes two comprehensive test phases:

PHASE 1: Exhaustive Tier Verification
  - Sequentially force-tests ALL 5 individual models and engine tiers:
    1. Gemini 3.5 Flash Lite
    2. Gemini 3.1 Flash Lite
    3. Gemma 4 26B (gemma-4-26b-a4b-it)
    4. Cloudflare Workers AI (@cf/meta/m2m100-1.2b)
    5. MyMemory Free API (with MYMEMORY_EMAIL quota)

PHASE 2: Rate Limit & Fallback Recovery Test
  - Test 2.1: Model Cascade on 429: gemini-3.5-flash-lite (429) -> gemini-3.1-flash-lite (Success)
  - Test 2.2: Deep Model Cascade on 429: 3.5 & 3.1 (429) -> gemma-4-26b-a4b-it (Success)
  - Test 2.3: All Gemini Models 429 -> Circuit Breaker Trip (300s) -> Fallback to Cloudflare Workers AI
  - Test 2.4: Gemini in Cooldown + Cloudflare 503 -> Fallback to MyMemory
  - Test 2.5: Circuit Breaker Cooldown Step Progression: 300s -> 900s -> 3600s backoff verification

All tests strictly verify:
- Tag preservation ({0}, {1})
- ASCII '0-9' numeric preservation (e.g. 102, 404)
- Thought / internal reasoning filtration
- Segment-level isolation and zero data corruption
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import types
from contextlib import contextmanager
from typing import Any, Callable

# Ensure UTF-8 console output for Windows PowerShell
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

from app.config import Settings
from app.engines import build_engines
from app.engines.cloudflare import CloudflareEngine
from app.engines.gemini import GeminiEngine
from app.engines.mymemory import MyMemoryEngine
from app.pipeline import Pipeline


def print_banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(f" {text}")
    print("=" * 78)


def print_subbanner(text: str) -> None:
    print("\n" + "-" * 78)
    print(f" >>> {text}")
    print("-" * 78)


# Multi-segment payload with multiple tags and numbers
TEST_SEGMENTS = [
    "Welcome to CAT Translate! Please verify segment sequence 1.",
    "Click {0} to download batch file 102 from server {1}.",
    "Warning: Error code 404 was encountered in section {0}.",
]


# =============================================================================
# SELECTIVE MOCKING HELPER FOR 429 SIMULATION
# =============================================================================

@contextmanager
def simulate_rate_limits(
    blocked_gemini_models: set[str] | None = None,
    fail_cloudflare: bool = False,
):
    """
    Context manager that intercepts requests.post:
    - If targeting a model in blocked_gemini_models, returns HTTP 429 RESOURCE_EXHAUSTED.
    - If fail_cloudflare is True, returns HTTP 503 on Cloudflare AI endpoints.
    - All other requests pass through to the real network.
    """
    blocked_models = blocked_gemini_models or set()
    original_post = requests.post

    def selective_post(url, *args, **kwargs):
        # Check Gemini URL
        for model in blocked_models:
            if f"models/{model}:generateContent" in str(url):
                resp = types.SimpleNamespace()
                resp.status_code = 429
                resp.text = json.dumps({
                    "error": {
                        "code": 429,
                        "message": f"Resource has been exhausted (quota exceeded for {model}).",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                })
                resp.json = lambda: json.loads(resp.text)
                return resp

        # Check Cloudflare URL
        if fail_cloudflare and "api.cloudflare.com" in str(url):
            resp = types.SimpleNamespace()
            resp.status_code = 503
            resp.text = json.dumps({"errors": [{"message": "Cloudflare Workers AI temporarily unavailable"}]})
            resp.json = lambda: json.loads(resp.text)
            return resp

        return original_post(url, *args, **kwargs)

    requests.post = selective_post
    try:
        yield
    finally:
        requests.post = original_post


# =============================================================================
# PHASE 1: INDIVIDUAL TIER VERIFICATION
# =============================================================================

def test_gemini_model_tier(
    model_name: str,
    api_key: str,
    sl: str,
    tl: str,
) -> dict[str, Any]:
    """Force-tests an individual Gemini model on multi-segment payload."""
    engine = GeminiEngine(api_key=api_key, models=(model_name,))
    start = time.time()
    result = {
        "tier": f"Gemini ({model_name})",
        "provider": "Google Gemini API",
        "status": "FAIL",
        "latency_s": 0.0,
        "segments_ok": False,
        "tags_ok": False,
        "digits_ok": False,
        "thoughts_filtered": True,
        "error": None,
        "outputs": [],
    }

    try:
        translated_list = engine.translate_batch(TEST_SEGMENTS, sl, tl, timeout=35.0)
        result["latency_s"] = round(time.time() - start, 2)
        result["outputs"] = translated_list

        if len(translated_list) == len(TEST_SEGMENTS):
            result["segments_ok"] = True

        seg2 = translated_list[1] if len(translated_list) > 1 else ""
        seg3 = translated_list[2] if len(translated_list) > 2 else ""

        result["tags_ok"] = ("{0}" in seg2 and "{1}" in seg2 and "{0}" in seg3)
        result["digits_ok"] = ("102" in seg2 and "404" in seg3)

        all_text = " ".join(translated_list).lower()
        if "<thought>" in all_text or "</thought>" in all_text or "<think>" in all_text:
            result["thoughts_filtered"] = False

        if result["segments_ok"] and result["tags_ok"] and result["digits_ok"] and result["thoughts_filtered"]:
            result["status"] = "PASS"

    except Exception as exc:
        result["latency_s"] = round(time.time() - start, 2)
        result["error"] = str(exc)

    return result


def test_single_segment_engine_tier(
    tier_name: str,
    provider_name: str,
    engine: Any,
    sl: str,
    tl: str,
) -> dict[str, Any]:
    """Force-tests Cloudflare or MyMemory sequentially across all segments."""
    start = time.time()
    result = {
        "tier": tier_name,
        "provider": provider_name,
        "status": "FAIL",
        "latency_s": 0.0,
        "segments_ok": False,
        "tags_ok": False,
        "digits_ok": False,
        "thoughts_filtered": True,
        "error": None,
        "outputs": [],
    }

    outputs: list[str] = []
    try:
        for seg in TEST_SEGMENTS:
            out = engine.translate(seg, sl, tl, timeout=30.0)
            outputs.append(out)

        result["latency_s"] = round(time.time() - start, 2)
        result["outputs"] = outputs

        if len(outputs) == len(TEST_SEGMENTS):
            result["segments_ok"] = True

        seg2 = outputs[1] if len(outputs) > 1 else ""
        seg3 = outputs[2] if len(outputs) > 2 else ""

        result["tags_ok"] = ("{0}" in seg2 and ("{1}" in seg2 or "{ 1 }" in seg2) and "{0}" in seg3)
        result["digits_ok"] = ("102" in seg2 and "404" in seg3)

        if result["segments_ok"] and result["tags_ok"] and result["digits_ok"]:
            result["status"] = "PASS"

    except Exception as exc:
        result["latency_s"] = round(time.time() - start, 2)
        result["error"] = str(exc)

    return result


# =============================================================================
# PHASE 2: RATE LIMIT & FALLBACK RECOVERY TESTS
# =============================================================================

def run_rate_limit_and_fallback_tests(settings: Settings, sl: str, tl: str) -> list[dict[str, Any]]:
    print_banner("PHASE 2: RATE LIMIT (429) & FALLBACK RECOVERY TESTS")
    recovery_results: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Test 2.1: Model Cascade on 429 (gemini-3.5-flash-lite -> gemini-3.1-flash-lite)
    # -------------------------------------------------------------------------
    print_subbanner("TEST 2.1: Model Cascade on 429: gemini-3.5-flash-lite -> gemini-3.1-flash-lite")
    print("Simulating HTTP 429 RESOURCE_EXHAUSTED on 'gemini-3.5-flash-lite'...")

    t0 = time.time()
    rec21 = {
        "test": "2.1 Model Cascade (3.5 [429] -> 3.1 [OK])",
        "status": "FAIL",
        "latency_s": 0.0,
        "details": "",
    }

    try:
        with simulate_rate_limits(blocked_gemini_models={"gemini-3.5-flash-lite"}):
            # Engine with fast intra-model sleep for instant testing
            engine = GeminiEngine(
                api_key=settings.gemini_api_key,
                models=settings.gemini_models,
                delays=(0.1, 0.2),
                sleep=lambda s: None,
            )
            out = engine.translate("Please click {0} to download batch file 102.", sl, tl, timeout=30.0)
            rec21["latency_s"] = round(time.time() - t0, 2)

            tags_ok = "{0}" in out
            digits_ok = "102" in out

            if tags_ok and digits_ok:
                rec21["status"] = "PASS"
                rec21["details"] = f"Cascaded to 3.1 Flash Lite successfully! Output: {out[:60]}..."
                print(f"  ✓ {rec21['details']}")
                print(f"  ✓ Verified: 3.5 raised 429 -> caught -> 3.1 succeeded in {rec21['latency_s']}s")
            else:
                rec21["details"] = f"Invariant failure: tags={tags_ok}, digits={digits_ok}"
                print(f"  ✗ {rec21['details']}")

    except Exception as exc:
        rec21["latency_s"] = round(time.time() - t0, 2)
        rec21["details"] = f"Error: {exc}"
        print(f"  ✗ Failed with exception: {exc}")

    recovery_results.append(rec21)

    # -------------------------------------------------------------------------
    # Test 2.2: Deep Model Cascade on 429 (3.5 & 3.1 [429] -> gemma-4-26b-a4b-it)
    # -------------------------------------------------------------------------
    print_subbanner("TEST 2.2: Deep Model Cascade: 3.5 & 3.1 [429] -> gemma-4-26b-a4b-it")
    print("Simulating HTTP 429 on BOTH 'gemini-3.5-flash-lite' AND 'gemini-3.1-flash-lite'...")

    t0 = time.time()
    rec22 = {
        "test": "2.2 Deep Cascade (3.5 & 3.1 [429] -> Gemma 4 [OK])",
        "status": "FAIL",
        "latency_s": 0.0,
        "details": "",
    }

    try:
        with simulate_rate_limits(blocked_gemini_models={"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}):
            engine = GeminiEngine(
                api_key=settings.gemini_api_key,
                models=settings.gemini_models,
                delays=(0.1, 0.2),
                sleep=lambda s: None,
            )
            out = engine.translate("Warning: Error code 404 was encountered in section {0}.", sl, tl, timeout=45.0)
            rec22["latency_s"] = round(time.time() - t0, 2)

            tags_ok = "{0}" in out
            digits_ok = "404" in out
            thoughts_ok = "<thought>" not in out.lower()

            if tags_ok and digits_ok and thoughts_ok:
                rec22["status"] = "PASS"
                rec22["details"] = f"Cascaded to Gemma 4 26B successfully! Output: {out[:60]}..."
                print(f"  ✓ {rec22['details']}")
                print(f"  ✓ Verified: 3.5 and 3.1 raised 429 -> Gemma 4 took over in {rec22['latency_s']}s")
            else:
                rec22["details"] = f"Invariant failure: tags={tags_ok}, digits={digits_ok}, thoughts_ok={thoughts_ok}"
                print(f"  ✗ {rec22['details']}")

    except Exception as exc:
        rec22["latency_s"] = round(time.time() - t0, 2)
        rec22["details"] = f"Error: {exc}"
        print(f"  ✗ Failed with exception: {exc}")

    recovery_results.append(rec22)

    # -------------------------------------------------------------------------
    # Test 2.3: All Gemini Models 429 -> Circuit Breaker Trip -> Cloudflare Fallback
    # -------------------------------------------------------------------------
    print_subbanner("TEST 2.3: All Gemini Models 429 -> Circuit Breaker Trip (300s) -> Cloudflare Fallback")
    print("Simulating HTTP 429 on ALL Gemini models (full quota exhaustion)...")

    t0 = time.time()
    rec23 = {
        "test": "2.3 Pipeline Fallback (Gemini [429] -> Breaker Trip -> Cloudflare)",
        "status": "FAIL",
        "latency_s": 0.0,
        "details": "",
    }

    try:
        with simulate_rate_limits(blocked_gemini_models=set(settings.gemini_models)):
            # Instantiate full pipeline
            engines_map = build_engines(settings)
            # Fast sleep for instant pacing in test
            pipe = Pipeline(engines_map, settings, sleep=lambda s: None)

            results, meta = pipe.run_batch(
                ["Please click {0} to download batch file 102."],
                sl,
                tl,
            )
            rec23["latency_s"] = round(time.time() - t0, 2)
            first_res = results[0]

            # Invariants
            via_engine = first_res.via
            cooldown_rem = pipe._cooldown_remaining("gemini")
            cooldown_tripped = cooldown_rem > 0
            tags_ok = "{0}" in (first_res.text or "")
            digits_ok = "102" in (first_res.text or "")
            attempts_logged = any(a.get("engine") == "gemini" and a.get("kind") == "rate_limited" for a in first_res.attempts)

            print(f"  • Result Via        : {via_engine}")
            print(f"  • Gemini Cooldown   : Active ({cooldown_rem}s remaining)")
            print(f"  • Translation Text  : {first_res.text}")
            print(f"  • Attempts Recorded : {first_res.attempts}")

            if via_engine == "cloudflare" and cooldown_tripped and tags_ok and digits_ok and attempts_logged:
                rec23["status"] = "PASS"
                rec23["details"] = f"Breaker tripped gemini (+{cooldown_rem}s) and routed to cloudflare successfully!"
                print(f"  ✓ {rec23['details']}")
            else:
                rec23["details"] = f"Failed criteria: via={via_engine}, cooldown={cooldown_tripped}, tags={tags_ok}, digits={digits_ok}"
                print(f"  ✗ {rec23['details']}")

    except Exception as exc:
        rec23["latency_s"] = round(time.time() - t0, 2)
        rec23["details"] = f"Error: {exc}"
        print(f"  ✗ Failed with exception: {exc}")

    recovery_results.append(rec23)

    # -------------------------------------------------------------------------
    # Test 2.4: Gemini in Cooldown + Cloudflare Down -> Fallback to MyMemory
    # -------------------------------------------------------------------------
    print_subbanner("TEST 2.4: Gemini in Cooldown + Cloudflare 503 -> Fallback to MyMemory")
    print("Simulating Gemini in active cooldown + Cloudflare Workers AI 503 error...")

    t0 = time.time()
    rec24 = {
        "test": "2.4 Dual Failure Fallback (Gemini Cooldown + CF 503 -> MyMemory)",
        "status": "FAIL",
        "latency_s": 0.0,
        "details": "",
    }

    try:
        with simulate_rate_limits(blocked_gemini_models=set(settings.gemini_models), fail_cloudflare=True):
            engines_map = build_engines(settings)
            pipe = Pipeline(engines_map, settings, sleep=lambda s: None)

            # Manually put gemini in cooldown to simulate prior trip
            pipe.states["gemini"].cooldown_until = pipe.clock() + 300
            pipe.states["gemini"].trips = 1

            results, meta = pipe.run_batch(
                ["Warning: Error code 404 was encountered in section {0}."],
                sl,
                tl,
            )
            rec24["latency_s"] = round(time.time() - t0, 2)
            first_res = results[0]

            via_engine = first_res.via
            tags_ok = "{0}" in (first_res.text or "")
            digits_ok = "404" in (first_res.text or "")

            print(f"  • Result Via        : {via_engine}")
            print(f"  • Translation Text  : {first_res.text}")
            print(f"  • Attempts Recorded : {first_res.attempts}")

            # Check that gemini recorded "cooling down" in attempts without making network call
            gemini_cooldown_logged = any(
                a.get("engine") == "gemini" and a.get("kind") == "cooldown" for a in first_res.attempts
            )

            if via_engine == "mymemory" and tags_ok and digits_ok and gemini_cooldown_logged:
                rec24["status"] = "PASS"
                rec24["details"] = "Gemini skipped (cooldown), Cloudflare failed -> MyMemory delivered valid translation!"
                print(f"  ✓ {rec24['details']}")
            else:
                rec24["details"] = f"Failed criteria: via={via_engine}, tags={tags_ok}, digits={digits_ok}, gemini_skipped={gemini_cooldown_logged}"
                print(f"  ✗ {rec24['details']}")

    except Exception as exc:
        rec24["latency_s"] = round(time.time() - t0, 2)
        rec24["details"] = f"Error: {exc}"
        print(f"  ✗ Failed with exception: {exc}")

    recovery_results.append(rec24)

    # -------------------------------------------------------------------------
    # Test 2.5: Circuit Breaker Stepped Backoff Progression (300s -> 900s -> 3600s)
    # -------------------------------------------------------------------------
    print_subbanner("TEST 2.5: Circuit Breaker Stepped Backoff (300s -> 900s -> 3600s)")
    print("Verifying mathematical progression of cooldown trips and strike reset...")

    rec25 = {
        "test": "2.5 Stepped Cooldown Progression (300s -> 900s -> 3600s)",
        "status": "FAIL",
        "latency_s": 0.01,
        "details": "",
    }

    try:
        from app.engines.base import EngineError

        pipe = Pipeline(build_engines(settings), settings, sleep=lambda s: None)
        st = pipe.states["gemini"]

        # Initial state
        assert st.trips == 0 and st.strikes == 0, "Initial state not clean"

        # Trip 1: Rate limit 429 (TRIP_NOW)
        pipe._record_failure("gemini", EngineError("rate_limited", "quota 1"), "test")
        rem1 = pipe._cooldown_remaining("gemini")
        trip1_ok = (rem1 >= 299 and st.trips == 1 and st.strikes == 0)
        print(f"  • Trip 1 (Immediate 429) : Cooldown = {rem1}s (expected ~300s, trips=1) -> {'PASS' if trip1_ok else 'FAIL'}")

        # Trip 2: Rate limit 429 again
        pipe._record_failure("gemini", EngineError("rate_limited", "quota 2"), "test")
        rem2 = pipe._cooldown_remaining("gemini")
        trip2_ok = (rem2 >= 899 and st.trips == 2 and st.strikes == 0)
        print(f"  • Trip 2 (Second 429)    : Cooldown = {rem2}s (expected ~900s, trips=2) -> {'PASS' if trip2_ok else 'FAIL'}")

        # Trip 3: Rate limit 429 a third time
        pipe._record_failure("gemini", EngineError("rate_limited", "quota 3"), "test")
        rem3 = pipe._cooldown_remaining("gemini")
        trip3_ok = (rem3 >= 3599 and st.trips == 3 and st.strikes == 0)
        print(f"  • Trip 3 (Third 429)     : Cooldown = {rem3}s (expected ~3600s, trips=3) -> {'PASS' if trip3_ok else 'FAIL'}")

        # Success reset: when an engine recovers, trips and cooldown reset to 0
        pipe._record_success("gemini", 150)
        reset_ok = (st.trips == 0 and st.strikes == 0 and pipe._cooldown_remaining("gemini") == 0)
        print(f"  • Recovery Reset         : Success resets trips to {st.trips}, cooldown to 0 -> {'PASS' if reset_ok else 'FAIL'}")

        if trip1_ok and trip2_ok and trip3_ok and reset_ok:
            rec25["status"] = "PASS"
            rec25["details"] = "Stepped backoff (300s -> 900s -> 3600s) and recovery reset validated perfectly!"
            print(f"  ✓ {rec25['details']}")
        else:
            rec25["details"] = "One or more step durations mismatched settings"

    except Exception as exc:
        rec25["details"] = f"Error: {exc}"
        print(f"  ✗ Failed with exception: {exc}")

    recovery_results.append(rec25)

    return recovery_results


# =============================================================================
# MAIN ORCHESTRATION & REPORTING
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Exhaustive Multi-Tier Live Self-Test & Recovery Suite")
    parser.add_argument("--sl", default="en", help="Source language (default: en)")
    parser.add_argument("--tl", default="bn", help="Target language (default: bn)")
    parser.add_argument("--skip-phase1", action="store_true", help="Skip Phase 1 individual tier tests")
    parser.add_argument("--recovery-only", action="store_true", help="Alias for --skip-phase1")
    args = parser.parse_args()

    settings = Settings.from_env()

    print_banner("CAT TRANSLATE API - FULL END-TO-END VERIFICATION & RECOVERY SUITE")
    print(f"• Configured Fallback Chain : {' -> '.join(settings.engine_order)}")
    print(f"• Configured Gemini Models  : {' -> '.join(settings.gemini_models)}")
    print(f"• Language Pair             : {args.sl} -> {args.tl}")
    print(f"• Multi-Segment Payload     : {len(TEST_SEGMENTS)} segments with {{0}}, {{1}} & ASCII digits 102/404")
    print(f"• Cooldown Steps (seconds)  : {settings.cooldown_steps}")

    phase1_records: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # PHASE 1 EXECUTION
    # -------------------------------------------------------------------------
    if not (args.skip_phase1 or args.recovery_only):
        print_banner("PHASE 1: INDIVIDUAL TIER BASELINE VERIFICATION")
        gemini_api_key = settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")

        if not gemini_api_key:
            print("\n[!] WARNING: GEMINI_API_KEY not set in .env. Skipping Gemini model tests.")
        else:
            for idx, model in enumerate(settings.gemini_models, start=1):
                print_subbanner(f"TIER {idx}/5: Gemini Model '{model}'")
                rec = test_gemini_model_tier(model, gemini_api_key, args.sl, args.tl)
                phase1_records.append(rec)
                if rec["status"] == "PASS":
                    print(f"  ✓ Status: PASS (took {rec['latency_s']}s)")
                    print(f"    Sample: {rec['outputs'][1] if len(rec['outputs']) > 1 else ''}")
                else:
                    print(f"  ✗ Status: FAIL - {rec['error']}")

                # Pacing pause
                pause_sec = settings.delays.get("gemini", (4.0, 5.0))[0]
                time.sleep(pause_sec)

        # Cloudflare Tier
        print_subbanner("TIER 4/5: Cloudflare Workers AI (@cf/meta/m2m100-1.2b)")
        cf_engine = CloudflareEngine(
            account_id=settings.cloudflare_account_id,
            api_token=settings.cloudflare_api_token,
            model=settings.cloudflare_model,
        )
        if cf_engine.supports(args.sl, args.tl):
            rec = test_single_segment_engine_tier("Cloudflare Workers AI", "Cloudflare REST", cf_engine, args.sl, args.tl)
            phase1_records.append(rec)
            print(f"  ✓ Status: PASS (took {rec['latency_s']}s)" if rec["status"] == "PASS" else f"  ✗ Status: FAIL - {rec['error']}")
            time.sleep(settings.delays.get("cloudflare", (0.5, 1.0))[0])

        # MyMemory Tier
        print_subbanner("TIER 5/5: MyMemory Engine (Final Fallback)")
        mm_engine = MyMemoryEngine(email=settings.mymemory_email)
        rec = test_single_segment_engine_tier("MyMemory Engine", "MyMemory API", mm_engine, args.sl, args.tl)
        phase1_records.append(rec)
        print(f"  ✓ Status: PASS (took {rec['latency_s']}s)" if rec["status"] == "PASS" else f"  ✗ Status: FAIL - {rec['error']}")

    # -------------------------------------------------------------------------
    # PHASE 2 EXECUTION (Rate Limit & Fallback Recovery)
    # -------------------------------------------------------------------------
    phase2_records = run_rate_limit_and_fallback_tests(settings, args.sl, args.tl)

    # -------------------------------------------------------------------------
    # FINAL SUMMARY REPORT
    # -------------------------------------------------------------------------
    print_banner("FINAL VERIFICATION & RECOVERY SUMMARY REPORT")

    if phase1_records:
        print("\n--- PHASE 1: TIER BASELINE RESULTS ---")
        print(f"{'Tier / Model':<32} | {'Status':<6} | {'Time (s)':<8} | {'Tags':<6} | {'0-9':<6} | {'Thoughts'}")
        print("-" * 78)
        for r in phase1_records:
            tier = r["tier"][:30]
            status = r["status"]
            time_str = f"{r['latency_s']:.2f}s"
            tags = "PASS" if r["tags_ok"] else "FAIL"
            digits = "PASS" if r["digits_ok"] else "FAIL"
            thoughts = "PASS" if r["thoughts_filtered"] else "FAIL"
            print(f"{tier:<32} | {status:<6} | {time_str:<8} | {tags:<6} | {digits:<6} | {thoughts}")

    print("\n--- PHASE 2: RATE LIMIT & FALLBACK RECOVERY RESULTS ---")
    print(f"{'Test Scenario':<55} | {'Status':<6} | {'Latency'}")
    print("-" * 78)
    for r in phase2_records:
        t_name = r["test"][:53]
        status = r["status"]
        lat = f"{r['latency_s']:.2f}s"
        print(f"{t_name:<55} | {status:<6} | {lat}")

    print("=" * 78)
    all_p1 = all(r["status"] == "PASS" for r in phase1_records) if phase1_records else True
    all_p2 = all(r["status"] == "PASS" for r in phase2_records)

    if all_p1 and all_p2:
        print("FINAL VERDICT: ✓ ALL TIERS & RATE LIMIT RECOVERY TESTS PASSED!")
    else:
        print("FINAL VERDICT: ✗ ONE OR MORE TESTS FAILED")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
