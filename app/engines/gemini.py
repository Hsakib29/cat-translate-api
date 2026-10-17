"""Google Gemini translation engine with model fallback, thought filtration, and numbered JSON validation."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable

import requests

from ..textutils import check_tags_preserved, normalize_ascii_digits, scrub
from .base import BLOCKED, EMPTY, ERROR, NETWORK, RATE_LIMITED, UNSUPPORTED, Engine, EngineError
from .errors import classify_exception

log = logging.getLogger("cat.engine.gemini")

DEFAULT_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-a4b-it",
)


def _strip_thought_blocks(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Extracts text from candidate parts, explicitly ignoring any parts or blocks
    flagged as 'thought' or internal reasoning (e.g. from Gemma 4 or Gemini Flash).
    Returns (cleaned_text, list_of_ignored_thoughts).
    """
    parts = candidate.get("content", {}).get("parts", [])
    thought_blocks: list[str] = []
    text_blocks: list[str] = []

    for part in parts:
        is_thought = part.get("thought") is True or part.get("type") == "thought"
        part_text = part.get("text", "")
        if is_thought:
            thought_blocks.append(part_text)
        else:
            text_blocks.append(part_text)

    raw_combined = "".join(text_blocks).strip()

    # Also detect and strip inline XML-like reasoning tags (<thought>...</thought> or <think>...</think>)
    inline_thoughts = re.findall(
        r"<(?:thought|think)>([\s\S]*?)</(?:thought|think)>", raw_combined, flags=re.IGNORECASE
    )
    thought_blocks.extend(inline_thoughts)
    cleaned = re.sub(
        r"<(?:thought|think)>[\s\S]*?</(?:thought|think)>", "", raw_combined, flags=re.IGNORECASE
    ).strip()

    # Strip markdown code blocks if present (```json ... ```)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    return cleaned.strip(), thought_blocks


def _build_system_instruction(sl: str, tl: str) -> str:
    target_guidance = ""
    if tl.lower() in ("bn", "ben", "bengali"):
        target_guidance = (
            "BENGALI (বাংলা) EDITORIAL GUIDELINES:\n"
            "- Produce natural, fluent, and idiomatic Bengali adhering to standard Subject-Object-Verb (SOV) structure.\n"
            "- Avoid rigid, mechanical word-for-word calques or awkward literal phrasing.\n"
            "- Use refined, polished, and standard formal register (মার্জিত প্রমিত চলিত বাংলা) suitable for scholarly journals, publications (e.g. EA Journal), and institutional reports.\n"
            "- Ensure contextual flow, coherent pronoun/tense references, and smooth transitions across adjacent segments in the sequence.\n"
            "- Professional terminology for headings, titles, and table headers should use standard, publication-grade equivalents (e.g., 'Executive Summary' -> 'নির্বাহী সারসংক্ষেপ', 'Table of Contents' -> 'সূচিপত্র', 'Editorial Board' -> 'সম্পাদকীয় পর্ষদ').\n\n"
        )

    return (
        f"You are an expert bilingual senior editor and professional translator for scholarly journals, periodicals, and Computer Assisted Translation (CAT) systems.\n"
        f"Translate the text values from source language '{sl}' into target language '{tl}' with utmost fluency, semantic accuracy, and natural readability.\n"
        f"The input is a JSON object mapping integer sequence IDs to source segments.\n"
        f"You MUST output a valid JSON object with the identical integer sequence IDs as keys.\n\n"
        f"{target_guidance}"
        f"STRICT INVARIANTS:\n"
        f"1. Retain every single sequence key without exception. Do not merge, reorder, or skip keys.\n"
        f"2. Preserve all placeholders, variables, and tags EXACTLY as they appear (e.g. {{0}}, {{ 1 }}, {{name}}, <br/>). Never alter, translate, or remove them.\n"
        f"3. Preserve all numerical digits inside placeholder tags or technical codes strictly as ASCII characters '0'-'9'.\n"
        f"4. Output raw JSON only. Do not wrap in markdown ```json ``` blocks. No introductory or concluding remarks."
    )


class GeminiEngine(Engine):
    name = "gemini"

    def __init__(
        self,
        api_key: str = "",
        models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
        delays: tuple[float, float] = (4.0, 5.0),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.models = tuple(models) if models else DEFAULT_MODELS
        self.delays = delays
        self.sleep = sleep

    def supports(self, sl: str, tl: str) -> bool:
        # Requires API key to be considered supported and available
        return bool(self.api_key.strip())

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        results = self.translate_batch([text], sl, tl, timeout)
        return results[0]

    def translate_batch(self, segments: list[str], sl: str, tl: str, timeout: float) -> list[str]:
        if not self.api_key.strip():
            raise EngineError(UNSUPPORTED, "GEMINI_API_KEY is not configured.")

        last_error: EngineError | None = None
        for idx, model in enumerate(self.models):
            # Respect inter-call delay if cycling between models inside Gemini
            if idx > 0:
                pause = self.delays[0] if self.delays else 4.0
                self.sleep(pause)

            try:
                out = self._call_model(model, segments, sl, tl, timeout)
                return out
            except EngineError as err:
                last_error = err
                log.warning("Gemini model %s failed: %s. Trying next model...", model, scrub(err.message))
            except Exception as exc:  # noqa: BLE001
                last_error = classify_exception(exc)
                log.warning("Gemini model %s error: %s. Trying next model...", model, scrub(str(exc)))

        if last_error:
            raise last_error
        raise EngineError(ERROR, "All Gemini models in GEMINI_MODELS failed.")

    def _call_model(self, model: str, segments: list[str], sl: str, tl: str, timeout: float) -> list[str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
        payload_map = {str(i): seg for i, seg in enumerate(segments, start=1)}
        prompt_text = json.dumps(payload_map, ensure_ascii=False)

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
                    {"text": _build_system_instruction(sl, tl)}
                ]
            },
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        try:
            resp = requests.post(url, json=body, timeout=timeout)
        except requests.exceptions.Timeout:
            raise EngineError(NETWORK, f"Gemini model {model} timed out after {timeout:.0f}s.") from None
        except requests.exceptions.RequestException as exc:
            raise classify_exception(exc) from None

        if resp.status_code == 429:
            raise EngineError(RATE_LIMITED, f"Gemini model {model} rate limited (HTTP 429).")
        if resp.status_code in (401, 403):
            raise EngineError(BLOCKED, f"Gemini API key rejected for model {model} (HTTP {resp.status_code}).")
        if resp.status_code >= 500:
            raise EngineError(ERROR, f"Gemini server error for model {model} (HTTP {resp.status_code}).")
        if resp.status_code != 200:
            raise EngineError(ERROR, f"Gemini HTTP {resp.status_code}: {scrub(resp.text)}")

        try:
            data = resp.json()
        except Exception:
            raise EngineError(EMPTY, f"Gemini model {model} returned non-JSON response.") from None

        candidates = data.get("candidates", [])
        if not candidates:
            raise EngineError(EMPTY, f"Gemini model {model} returned no candidates.")

        clean_text, _ = _strip_thought_blocks(candidates[0])
        if not clean_text:
            raise EngineError(EMPTY, f"Gemini model {model} returned empty translation after thought filtration.")

        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            raise EngineError(EMPTY, f"Gemini model {model} returned invalid JSON: {clean_text[:100]}") from None

        if not isinstance(parsed, dict):
            raise EngineError(EMPTY, f"Gemini model {model} output is not a JSON dictionary.")

        if len(parsed) != len(segments):
            raise EngineError(
                EMPTY,
                f"Gemini model {model} segment count mismatch: expected {len(segments)}, got {len(parsed)}."
            )

        results: list[str] = []
        for i, source_text in enumerate(segments, start=1):
            key = str(i)
            if key not in parsed:
                raise EngineError(EMPTY, f"Gemini model {model} output missing sequence key '{key}'.")

            translated_raw = str(parsed[key]).strip()
            if not translated_raw:
                raise EngineError(EMPTY, f"Gemini model {model} returned empty segment text for '{key}'.")

            # Normalize digits to ASCII 0-9
            normalized = normalize_ascii_digits(translated_raw)

            # Validate tag preservation (e.g. {0})
            tags_ok, missing_tag = check_tags_preserved(source_text, normalized)
            if not tags_ok:
                raise EngineError(EMPTY, f"Gemini model {model} lost tag '{missing_tag}' in translation.")

            results.append(normalized)

        return results
