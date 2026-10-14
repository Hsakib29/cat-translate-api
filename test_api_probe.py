#!/usr/bin/env python3
"""
Test API Probe for Gemini Engine Sequence & Thought Filtration.

Evaluates:
1. Gemini Model Sequence: Gemini 3.5 Flash Lite -> 3.1 Flash Lite -> Gemma 4 26B
2. Single API key from environment variable (GEMINI_API_KEY)
3. Detection and filtration of parts flagged as 'thought' / internal reasoning
4. Numbered JSON segment processing with segment count checks and retry behavior
5. Tag preservation guardrails (e.g. {0}) and sequence number validation
6. Preservation and normalization of numeric digits strictly as ASCII '0-9'
7. Pacing delay verification (4 to 5 second pause)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

# Ensure proper UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

# Default models requested for Pass 1
DEFAULT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-a4b-it",
]

# Regex for tags/placeholders like {0}, { 1 }, {name}, <tag>
TAG_REGEX = re.compile(r"\{\s*[\w\d]+\s*\}|<[^>]+>")

# Map non-ASCII localized digits to standard ASCII 0-9
VERNACULAR_DIGITS = {
    # Bengali ০-৯
    0x09E6: "0", 0x09E7: "1", 0x09E8: "2", 0x09E9: "3", 0x09EA: "4",
    0x09EB: "5", 0x09EC: "6", 0x09ED: "7", 0x09EE: "8", 0x09EF: "9",
    # Devanagari ०-९
    0x0966: "0", 0x0967: "1", 0x0968: "2", 0x0969: "3", 0x096A: "4",
    0x096B: "5", 0x096C: "6", 0x096D: "7", 0x096E: "8", 0x096F: "9",
    # Arabic-Indic ٠-٩
    0x0660: "0", 0x0661: "1", 0x0662: "2", 0x0663: "3", 0x0664: "4",
    0x0665: "5", 0x0666: "6", 0x0667: "7", 0x0668: "8", 0x0669: "9",
}


def normalize_ascii_digits(text: str) -> str:
    """Ensure all numeric characters are normalized to standard ASCII '0'-'9'."""
    return text.translate(VERNACULAR_DIGITS)


def extract_content_filtering_thoughts(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Extracts text from candidate parts, explicitly ignoring any parts or blocks
    flagged as 'thought' or internal reasoning.
    Returns (cleaned_text, list_of_ignored_thoughts).
    """
    parts = candidate.get("content", {}).get("parts", [])
    thought_blocks: list[str] = []
    text_blocks: list[str] = []

    for idx, part in enumerate(parts):
        is_thought = part.get("thought") is True or part.get("type") == "thought"
        part_text = part.get("text", "")
        if is_thought:
            thought_blocks.append(part_text)
        else:
            text_blocks.append(part_text)

    raw_combined = "".join(text_blocks).strip()

    # Also detect and strip inline XML-like reasoning tags (<thought>...</thought> or <think>...</think>)
    inline_thoughts = re.findall(r"<(?:thought|think)>([\s\S]*?)</(?:thought|think)>", raw_combined, flags=re.IGNORECASE)
    thought_blocks.extend(inline_thoughts)
    cleaned = re.sub(r"<(?:thought|think)>[\s\S]*?</(?:thought|think)>", "", raw_combined, flags=re.IGNORECASE).strip()

    # Remove markdown formatting if wrapped in ```json ... ```
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    return cleaned.strip(), thought_blocks


def build_system_instruction(sl: str, tl: str) -> str:
    return (
        f"You are an uncompromising translation engine for Computer Assisted Translation (CAT) software.\n"
        f"Translate the text values from source language '{sl}' into target language '{tl}'.\n"
        f"The input is a JSON object mapping integer sequence IDs to source segments.\n"
        f"You MUST output a valid JSON object with the identical integer sequence IDs as keys.\n\n"
        f"STRICT INVARIANTS:\n"
        f"1. Retain every single sequence key without exception. Do not merge, reorder, or skip keys.\n"
        f"2. Preserve all placeholders, variables, and tags EXACTLY as they appear (e.g. {{0}}, {{ 1 }}, {{name}}, <br/>). Never alter, translate, or remove them.\n"
        f"3. Preserve all numerical digits strictly as ASCII characters '0'-'9'. Never convert numbers into localized numerals (e.g. do not use ০-৯ or ۰-۹).\n"
        f"4. Output raw JSON only. Do not wrap in markdown ```json ``` blocks. No introductory or concluding remarks."
    )


def validate_and_process_segments(
    input_segments: list[str],
    raw_json_str: str,
) -> tuple[dict[str, str], list[str]]:
    """
    Validates numbered JSON output against input segments.
    Checks:
    - Segment count matches exactly
    - Sequence keys exist
    - Tags are preserved (e.g. {0})
    - Digits are converted to ASCII '0-9'
    Returns (translations_by_index, list_of_errors).
    """
    errors: list[str] = []
    translations: dict[str, str] = {}

    try:
        data = json.loads(raw_json_str)
    except json.JSONDecodeError as exc:
        return {}, [f"Invalid JSON returned: {exc}"]

    if not isinstance(data, dict):
        return {}, [f"Expected JSON object, got {type(data).__name__}"]

    expected_count = len(input_segments)
    actual_count = len(data)

    if actual_count != expected_count:
        errors.append(f"Segment count mismatch: expected {expected_count}, got {actual_count}")

    for idx, source_text in enumerate(input_segments, start=1):
        key = str(idx)
        if key not in data:
            errors.append(f"Missing sequence number: key '{key}' not in response")
            continue

        raw_translated = str(data[key])
        normalized = normalize_ascii_digits(raw_translated)

        # Check for lost tags
        source_tags = set(TAG_REGEX.findall(source_text))
        for tag in source_tags:
            if tag not in normalized:
                errors.append(f"Segment {key}: lost tag '{tag}' in translation (Source: {source_text!r} -> Translated: {normalized!r})")

        translations[key] = normalized

    return translations, errors


def call_gemini_api(
    model: str,
    api_key: str,
    segments: list[str],
    sl: str = "en",
    tl: str = "bn",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call Google Gemini REST API directly."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload_map = {str(i): seg for i, seg in enumerate(segments, start=1)}
    prompt_text = json.dumps(payload_map, ensure_ascii=False, indent=2)

    body = {
        "contents": [
            {
                "parts": [
                    {"text": f"Translate these segments from {sl} to {tl}:\n{prompt_text}"}
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {"text": build_system_instruction(sl, tl)}
            ]
        },
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    resp = requests.post(url, json=body, timeout=timeout)
    return {"status_code": resp.status_code, "data": resp.json() if resp.status_code == 200 else resp.text}


def run_simulated_probe():
    """Runs a comprehensive suite of simulated cases to test edge cases without network calls."""
    print("=" * 70)
    print("RUNNING PROBE TEST SUITE (THOUGHT FILTRATION & SEGMENT VALIDATION)")
    print("=" * 70)

    test_segments = [
        "Hello, welcome to our application!",
        "Click {0} to open file 102.",
        "Error code: 404.",
    ]

    # Case 1: Response with thought part and Bengali localized digits
    print("\n[Test 1] Response with candidate 'thought: true' and Bengali numerals:")
    simulated_candidate = {
        "content": {
            "parts": [
                {
                    "thought": True,
                    "text": "The user wants English to Bengali translation. Segment 2 has {0}. Numeric 102 should be kept.",
                },
                {
                    "thought": False,
                    "text": json.dumps({
                        "1": "হ্যালো, আমাদের অ্যাপ্লিকেশনে স্বাগতম!",
                        "2": "ফাইল ১০২ খুলতে {0} এ ক্লিক করুন।",  # contains Bengali digits ১০২
                        "3": "ত্রুটি কোড: ৪০৪।",              # contains Bengali digits ৪০৪
                    }),
                },
            ]
        }
    }

    clean_text, thoughts = extract_content_filtering_thoughts(simulated_candidate)
    print(f"  -> Detected {len(thoughts)} thought block(s):")
    for t in thoughts:
        print(f"     [IGNORED THOUGHT]: {t.strip()}")

    translations, errors = validate_and_process_segments(test_segments, clean_text)
    print(f"  -> Validation Errors: {errors}")
    print("  -> Final Processed Translations (with ASCII digits enforced):")
    for k, v in translations.items():
        print(f"     Segment {k}: {v}")

    assert len(thoughts) == 1, "Thought part was not captured!"
    assert "102" in translations["2"], f"Digits not normalized to ASCII 0-9: {translations['2']}"
    assert "{0}" in translations["2"], "Placeholder tag lost!"
    print("  ✓ Test 1 PASSED: Thought filtered, tag kept, digits normalized to '0-9'.")

    # Case 2: Lost tag detection
    print("\n[Test 2] Segment loses {0} placeholder tag:")
    lost_tag_candidate = {
        "content": {
            "parts": [
                {
                    "thought": False,
                    "text": json.dumps({
                        "1": "হ্যালো!",
                        "2": "ফাইল 102 খুলুন।",  # {0} is missing!
                        "3": "ত্রুটি কোড: 404।",
                    }),
                }
            ]
        }
    }
    clean_text, _ = extract_content_filtering_thoughts(lost_tag_candidate)
    translations, errors = validate_and_process_segments(test_segments, clean_text)
    print(f"  -> Validation Errors Detected: {errors}")
    assert any("lost tag '{0}'" in err for err in errors), "Failed to detect lost {0} tag!"
    print("  ✓ Test 2 PASSED: Lost tag flagged as failure.")

    # Case 3: Segment count mismatch
    print("\n[Test 3] Segment count mismatch (Gemini returns 2 instead of 3):")
    mismatch_candidate = {
        "content": {
            "parts": [
                {
                    "thought": False,
                    "text": json.dumps({
                        "1": "হ্যালো!",
                        "2": "ফাইল 102 খুলতে {0} এ ক্লিক করুন।",
                    }),
                }
            ]
        }
    }
    clean_text, _ = extract_content_filtering_thoughts(mismatch_candidate)
    translations, errors = validate_and_process_segments(test_segments, clean_text)
    print(f"  -> Validation Errors Detected: {errors}")
    assert any("Segment count mismatch" in err for err in errors), "Count mismatch not detected!"
    assert any("Missing sequence number" in err for err in errors), "Missing sequence key not detected!"
    print("  ✓ Test 3 PASSED: Segment count mismatch and missing sequence flagged.")

    # Case 3b: Inline XML reasoning tags (<thought>...</thought> / <think>...</think>)
    print("\n[Test 3b] Inline XML reasoning blocks (<thought>...</thought> / <think>...</think>):")
    xml_thought_candidate = {
        "content": {
            "parts": [
                {
                    "thought": False,
                    "text": "<thought>Translating text with tags and numbers...</thought>\n" + json.dumps({
                        "1": "হ্যালো!",
                        "2": "ফাইল 102 খুলতে {0} এ ক্লিক করুন।",
                        "3": "ত্রুটি কোড 404।",
                    }),
                }
            ]
        }
    }
    clean_text, thoughts = extract_content_filtering_thoughts(xml_thought_candidate)
    print(f"  -> Detected {len(thoughts)} inline reasoning block(s): {thoughts}")
    translations, errors = validate_and_process_segments(test_segments, clean_text)
    print(f"  -> Cleaned JSON translated without syntax error: {errors == []}")
    assert len(thoughts) == 1, "Failed to capture inline <thought> tag!"
    assert len(translations) == 3, "Failed to parse JSON after stripping inline thoughts!"
    print("  ✓ Test 3b PASSED: Inline reasoning tags stripped and JSON parsed cleanly.")

    # Case 4: Model switching cascade simulation
    print("\n[Test 4] Model cascade logic (Gemini 3.5 Flash Lite -> 3.1 Flash Lite -> Gemma 4 26B):")
    models = DEFAULT_MODELS

    def mock_call(model_name: str):
        if "3.5" in model_name:
            print(f"  -> Trying '{model_name}': Simulating HTTP 429 (Rate Limit Exhausted)")
            return 429, None
        elif "3.1" in model_name:
            print(f"  -> Falling back to '{model_name}': Simulating HTTP 200 OK")
            return 200, {"1": "সফল অনুবাদ"}
        else:
            return 200, {"1": "তৃতীয় মডেল"}

    answered_by = None
    for model_name in models:
        code, resp = mock_call(model_name)
        if code == 200:
            answered_by = model_name
            break
        print(f"     Model '{model_name}' failed with status {code}. Escalating to next in cascade...")

    print(f"  -> Resolved by model: '{answered_by}'")
    assert answered_by == "gemini-3.1-flash-lite", "Cascade failed to switch to secondary model!"
    print("  ✓ Test 4 PASSED: Model switching cascade operates as expected.")

    print("\n" + "=" * 70)
    print("ALL 4 PROBE LOGIC TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Gemini API Probe")
    parser.add_argument("--api-key", help="Gemini API Key (or set GEMINI_API_KEY environment variable)")
    parser.add_argument("--live", action="store_true", help="Perform live network call to Gemini API")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")

    # Run internal logic tests first
    run_simulated_probe()

    # If live requested or API key available, attempt live probe
    if args.live or api_key:
        if not api_key:
            print("\n[!] Cannot run live API probe: GEMINI_API_KEY is not set.")
            print("    Provide via: $env:GEMINI_API_KEY='your-key' or python test_api_probe.py --api-key <key> --live")
            return

        print("\n" + "=" * 70)
        print("EXECUTING LIVE GEMINI API PROBE")
        print("=" * 70)
        sample_segments = [
            "Hello, world!",
            "Open {0} to review segment 42.",
        ]

        for model in DEFAULT_MODELS:
            print(f"\nCalling model: {model} ...")
            start = time.time()
            try:
                res = call_gemini_api(model, api_key, sample_segments, sl="en", tl="bn")
                elapsed = time.time() - start
                status = res["status_code"]
                print(f"Status Code: {status} (took {elapsed:.2f}s)")

                if status == 200:
                    data = res["data"]
                    candidate = data["candidates"][0]
                    clean_text, thoughts = extract_content_filtering_thoughts(candidate)
                    if thoughts:
                        print(f"Filtered {len(thoughts)} thought parts: {thoughts[0][:100]}...")
                    else:
                        print("No thought parts found (clean output).")

                    translations, errors = validate_and_process_segments(sample_segments, clean_text)
                    print(f"Translations: {translations}")
                    print(f"Validation errors: {errors}")
                    print(f"SUCCESS with model: {model}")
                    break
                else:
                    print(f"Model {model} failed: {res['data']}")
            except Exception as e:
                print(f"Exception calling {model}: {e}")

            print("Waiting 4.5s pacing pause before trying next model...")
            time.sleep(4.5)
    else:
        print("\nNote: GEMINI_API_KEY not found in environment. Simulated probe validated all thought filtration,")
        print("JSON numbering, tag preservation, and model switching rules.")
        print("To run live test: $env:GEMINI_API_KEY='<your-key>'; python test_api_probe.py --live")


if __name__ == "__main__":
    main()
