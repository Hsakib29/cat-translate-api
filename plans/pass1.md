# Architecture Blueprint: Pass 1 Implementation

**Document:** `/plans/pass1.md`  
**Author:** Technical Architect  
**Status:** Ready for Review & Implementation  
**Target:** `cat-translate-api` (Pass 1)

---

## 1. Executive Summary & Architectural Objectives

Pass 1 upgrades the `cat-translate-api` from an unauthenticated web scraper proxy to a hardened, enterprise-ready translation pipeline backed primarily by large language model (LLM) intelligence (Gemini) and robust serverless fallback infrastructure (Cloudflare Workers AI + MyMemory).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       POST /api/translate Request                           │
│             {"sl": "en", "tl": "bn", "segments": ["Hello {0}"]}             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
                     ┌───────────────────────────────────┐
                     │     Pipeline Queue & Pacing       │
                     │   Single-Worker Concurrency Lock   │
                     └─────────────────┬─────────────────┘
                                       │
                                       ▼
        ┌─────────────────────────────────────────────────────────────┐
        │                 Default Fallback Chain                      │
        │                                                             │
        │  1. Gemini Engine (gemini)                                  │
        │     • Pacing: 4.0s - 5.0s pause                             │
        │     • Internal Model Switching Cascade:                     │
        │       Gemini 3.5 Flash Lite ──► 3.1 Flash Lite ──► Gemma 4  │
        │     • Numbered JSON segment processing                      │
        │     • Thought part filtration & ASCII '0-9' enforcement     │
        │     • Tag preservation validation ({0})                     │
        │                                │ (on failure / breaker)     │
        │                                ▼                            │
        │  2. Cloudflare Engine (cloudflare)                          │
        │     • Cloudflare Workers AI (@cf/meta/m2m100-1.2b)          │
        │     • Pacing: 0.5s - 1.0s pause                             │
        │                                │ (on failure / breaker)     │
        │                                ▼                            │
        │  3. MyMemory Engine (mymemory)                              │
        │     • Free API with MYMEMORY_EMAIL quota identification     │
        │     • Pacing: 0.5s - 1.0s pause                             │
        │                                                             │
        │  [Disabled by default]: google_web, google_gtx (opt-in only)│
        └─────────────────────────────────────────────────────────────┘
```

### Core Invariants Maintained
1. **Stateless CAT Proxy**: No glossary, user state, or translation memory persisted on the server.
2. **Segment-Level Failure Isolation**: One failed segment returns honest diagnostic attempts and `retryable: true` without aborting the rest of the batch.
3. **Strict Privacy**: Zero segment text logged or printed. All errors scrubbed via `scrub()`.
4. **Single-Worker Concurrency & Pacing**: Prevents upstream rate limits and bot flags.
5. **Deterministic Offline Tests**: Zero live external network calls during automated testing.

---

## 2. System Configuration & Environment Specification

### 2.1 New & Modified Environment Variables

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `GEMINI_API_KEY` | `string` | *Required for Gemini* | Single Google Gemini API key. Strict single-key loading (no key rotation). |
| `GEMINI_MODELS` | `string` | `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-26b` | Ordered model fallback cascade within the Gemini engine. |
| `CLOUDFLARE_ACCOUNT_ID` | `string` | `""` | Cloudflare account ID for Workers AI REST endpoint. |
| `CLOUDFLARE_API_TOKEN` | `string` | `""` | Cloudflare API bearer token with Workers AI read/run permissions. |
| `CLOUDFLARE_MODEL` | `string` | `@cf/meta/m2m100-1.2b` | Cloudflare Workers AI translation model ID. |
| `MYMEMORY_EMAIL` | `string` | `""` | Optional email passed to MyMemory API to increase daily free quota from 5k to 50k chars/day. |
| `ENGINE_ORDER` | `string` | `gemini,cloudflare,mymemory` | New default engine hierarchy. Google scrapers removed from default. |
| `ENGINE_DELAYS` | `string` | `gemini:4.0-5.0,cloudflare:0.5-1.0,mymemory:0.5-1.0` | Per-engine inter-call pause configuration. |

### 2.2 File Modifications
- **[app/config.py](file:///e:/Web%20Design/Antigravity/cat-translate-api/app/config.py)**:
  - Add `gemini_api_key`, `gemini_models`, `cloudflare_account_id`, `cloudflare_api_token`, `cloudflare_model`, and `mymemory_email` to the `Settings` dataclass.
  - Update `DEFAULT_ORDER` to `("gemini", "cloudflare", "mymemory")`.
  - Update `DEFAULT_DELAYS` with `"gemini": (4.0, 5.0)`.
  - Parse `GEMINI_MODELS` as a tuple of strings.
- **[.env.example](file:///e:/Web%20Design/Antigravity/cat-translate-api/.env.example)**:
  - Document all new environment variables with clear security advice.

---

## 3. Gemini Translation Engine Specification

### 3.1 Architecture & Design Decisions
- **REST via `requests`**: Direct integration against `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}`.
  - *Rationale*: Avoids massive dependencies (`google-genai` / `protobuf` / `grpcio`), guarantees low Docker build times, and permits deterministic unit mocking.
- **Single API Key Guarantee**: Key is read directly from `Settings.gemini_api_key`. Multi-account rotation is explicitly forbidden as requested.

### 3.2 Model Switching Cascade
The engine maintains a priority list of models:
1. `gemini-3.5-flash-lite` (Primary)
2. `gemini-3.1-flash-lite` (Secondary)
3. `gemma-4-26b` (Tertiary fallback)

**Switching Rules:**
- If Model 1 raises `rate_limited` (HTTP 429), `blocked` (content safety filter), `empty`, or network timeout, the engine automatically attempts Model 2, then Model 3.
- Only if **all three models fail** does `GeminiEngine` raise an `EngineError` to trip the pipeline fallback to Cloudflare.
- Rate-limited models are temporarily avoided or recorded in attempt telemetry.

### 3.3 Numbered JSON Segment Protocol
To prevent LLM hallucination, segment omissions, or format drift, segments are exchanged using a strict numbered JSON schema:

**Input Payload Prompt:**
```json
{
  "1": "Hello, how are you?",
  "2": "Please verify {0} before submitting.",
  "3": "Exit code 102."
}
```

**System Instruction:**
```text
You are an uncompromising translation engine for Computer Assisted Translation (CAT) software.
Translate the text values from source language '{sl}' into target language '{tl}'.
The input is a JSON object mapping string integer sequence IDs to source segments.
You MUST output a valid JSON object with the identical integer sequence IDs as keys.

STRICT INVARIANTS:
1. Retain every single sequence key without exception. Do not merge, reorder, or skip keys.
2. Preserve all placeholders, variables, and tags EXACTLY as they appear (e.g. {0}, { 1 }, {name}, <br/>). Never alter, translate, or remove them.
3. Preserve all numerical digits strictly as ASCII characters '0'-'9'. Never convert numbers into localized or vernacular numerals (e.g., do not use ০-৯ or ۰-۹).
4. Output raw JSON only. Do not wrap in markdown ```json ``` blocks. No introductory or concluding remarks.
```

### 3.4 Handling Thought Tokens
Newer Gemini Flash models output reasoning tokens.
- **Payload Parsing Rule**: Inspect `candidates[0].content.parts`.
- **Ignore Thought Parts**: If `part.get("thought") is True` or `part.get("type") == "thought"`, discard it completely.
- Extract only text from non-thought parts.
- If raw text contains `<thought>...</thought>` or `<think>...</think>` tags, strip them with regex before JSON parsing.

### 3.5 Numeric Digit Preservation (ASCII '0-9')
In many language pairs (e.g. English -> Bengali/Hindi/Arabic), translation models tend to translate ASCII `0-9` into native script numerals (`০-৯`, `०-९`).
- **Prompt Guard**: Explicit rule prohibiting numeral translation.
- **Post-Processing Normalizer**: If any vernacular numerals match known digit maps (Bengali `\u09e6-\u09ef`, Devanagari `\u0966-\u096f`, Arabic `\u0660-\u0669`), convert them back to ASCII `0-9` automatically before validation.

### 3.6 Pacing
- Configured in `DEFAULT_DELAYS["gemini"] = (4.0, 5.0)`.
- Enforced automatically by `Pipeline._pace("gemini")` before every Gemini API interaction.

---

## 4. Segment & Output Validation Guardrails

Validation must occur before any segment is accepted as successful.

```
                    ┌────────────────────────────┐
                    │    Gemini JSON Response    │
                    └──────────────┬─────────────┘
                                   │
                                   ▼
                    ┌────────────────────────────┐
                    │   Segment Count Check:     │
                    │   len(output) == len(input)│
                    └──────────────┬─────────────┘
                                   │ No ──► Trigger Retry / Raise EngineError
                                   ▼ Yes
                    ┌────────────────────────────┐
                    │   Sequence Key Check:      │
                    │   Every "k" in input exists│
                    └──────────────┬─────────────┘
                                   │ No ──► Mark Segment Failed
                                   ▼ Yes
                    ┌────────────────────────────┐
                    │   Tag Preservation Check:  │
                    │   All tags in source (e.g. │
                    │   {0}) present in target?  │
                    └──────────────┬─────────────┘
                                   │ No ──► Mark Segment Failed ("lost tag {0}")
                                   ▼ Yes
                    ┌────────────────────────────┐
                    │   Numeric Digit Check:     │
                    │   All digits normalized to │
                    │   ASCII 0-9                │
                    └──────────────┬─────────────┘
                                   │
                                   ▼
                    ┌────────────────────────────┐
                    │  Valid Result -> Pipeline  │
                    └────────────────────────────┘
```

### 4.1 Tag Preservation Validation
- Extract tags from source text using regex: `re.findall(r"\{\s*[0-9\w]+\s*\}", source_text)` + HTML tags `re.findall(r"<[^>]+>", source_text)`.
- For each tag found in `source_text`, assert that it appears in `translated_text`.
- If any tag is missing or altered:
  - Reject the translation for that specific segment.
  - Record failure reason: `lost tag '{tag}'`.
  - Segment falls back to the next engine in the chain.

### 4.2 Sequence Number Validation
- Ensure every input key (e.g. `"1"`, `"2"`) exists in the returned JSON object.
- If an output key is missing:
  - Mark that individual segment as failed (`missing sequence number '{key}'`).
  - Remaining valid segments are preserved.

### 4.3 Segment Count Check & Retry Logic
- If the total count of keys in the returned JSON does not match the request segment count:
  - Trigger one immediate retry against the next model in the Gemini cascade.
  - If still mismatched, flag unresolvable segments with `EngineError("empty", "Segment count mismatch from Gemini")`.

---

## 5. Fallback Engines & Registry Updates

### 5.1 Cloudflare Workers AI Engine (`app/engines/cloudflare.py`)
- **Class**: `CloudflareEngine(Engine)`
- **Name**: `"cloudflare"`
- **Endpoint**: `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}`
- **Model**: `@cf/meta/m2m100-1.2b` (supports 100+ languages directly).
- **Authentication**: `Authorization: Bearer {CLOUDFLARE_API_TOKEN}`.
- **Error Mapping**:
  - HTTP 401/403 -> `EngineError("blocked", "Cloudflare auth failed")`
  - HTTP 429 -> `EngineError("rate_limited", "Cloudflare quota/rate limit")`
  - HTTP 500/503 -> `EngineError("error", "Cloudflare AI service error")`
  - Connection timeout -> `EngineError("network", "Cloudflare timeout")`
- **Pacing**: `(0.5, 1.0)` seconds.

### 5.2 MyMemory Engine Updates (`app/engines/mymemory.py`)
- Read `MYMEMORY_EMAIL` from `Settings` / environment.
- Pass `email=settings.mymemory_email` to `deep_translator.MyMemoryTranslator(source=src, target=tgt, email=email)`.
- If using direct REST fallback, append `&de={MYMEMORY_EMAIL}`.
- Maintain status as the final safety net in the fallback chain.

### 5.3 Disabling Google Scraping Engines
- Remove `GoogleWebEngine` and `GoogleGtxEngine` from `DEFAULT_ORDER`.
- Retain classes in `ENGINE_CLASSES` so callers can explicitly request them via `POST /api/translate` with `{"engines": ["google_web"]}` if desired, but never by default.

---

## 6. Testing & Pytest Mocking Strategy

All tests must run offline, deterministically, and with zero live network calls.

### 6.1 Mocking Matrix

| Test Scope | Target Function / Module | Mock Strategy | Expected Behavior Verified |
| :--- | :--- | :--- | :--- |
| **Gemini Success** | `requests.post` | Return HTTP 200 with JSON parts: `{"1": "অনুবাদ"}` | Returns translated text with `via: "gemini"`. |
| **Gemini Thought Filtration** | `requests.post` | Candidate part has `{"thought": True, "text": "thinking"}` and `{"thought": False, "text": "{\"1\": \"output\"}"}` | Only `"output"` is captured; thought text is discarded. |
| **Gemini Model Cascade** | `requests.post` | Call 1 (3.5 Flash Lite) -> HTTP 429; Call 2 (3.1 Flash Lite) -> HTTP 200 | Cascade switches model automatically without tripping pipeline. |
| **Gemini Lost Tag** | `requests.post` | Input: `"Hello {0}"`; Output: `{"1": "হ্যালো"}` (missing `{0}`) | Segment rejected with `lost tag {0}`; triggers fallback to Cloudflare. |
| **Gemini Missing Sequence** | `requests.post` | Input: 2 segments; Output: `{"1": "one"}` | Segment 2 marked as failed with missing sequence error. |
| **Digit Preservation** | `requests.post` | Output contains Bengali digits `১০০` | Converted back to ASCII `100`. |
| **Gemini 4-5s Pacing** | `Pipeline._pace` with `FakeClock` | `clock.sleeps` contains values in `[4.0, 5.0]` | Pacing delay verified via `FakeClock`. |
| **Cloudflare Workers AI** | `requests.post` | Return HTTP 200 with `{"result": {"translated_text": "text"}}` | Correctly parses translation. |
| **Full Fallback Chain** | `requests.post` | Gemini fails -> Cloudflare fails -> MyMemory succeeds | Result attribution is `via: "mymemory"`, attempts record reasons for all 3. |

### 6.2 Implementation of Test Helpers
- Add `fake_gemini_response(content_dict, thought_text=None)` in [tests/helpers.py](file:///e:/Web%20Design/Antigravity/cat-translate-api/tests/helpers.py).
- Author dedicated test suite [tests/test_gemini.py](file:///e:/Web%20Design/Antigravity/cat-translate-api/tests/test_gemini.py) and [tests/test_cloudflare.py](file:///e:/Web%20Design/Antigravity/cat-translate-api/tests/test_cloudflare.py).

---

## 7. Step-by-Step Implementation Sequence (For Engineer)

```
Step 1: Configuration & Environment
 ├── Update app/config.py (Add Gemini, Cloudflare, MyMemory email settings)
 └── Update .env.example with documentation

Step 2: Validation & Normalization Utilities
 ├── Enhance app/textutils.py (Tag extraction, ASCII digit normalizer)
 └── Unit test digit normalizer and tag matching

Step 3: Engine Implementations
 ├── Create app/engines/gemini.py (Model cascade, numbered JSON, thought filter)
 ├── Create app/engines/cloudflare.py (Workers AI client)
 ├── Update app/engines/mymemory.py (Inject MYMEMORY_EMAIL)
 └── Update app/engines/__init__.py (Register engines, update defaults)

Step 4: Pipeline Integration & Pacing
 ├── Verify Pipeline._pace respects (4.0, 5.0) for Gemini
 └── Verify fallback escalation from gemini -> cloudflare -> mymemory

Step 5: Testing Suite Execution (Tester)
 ├── Author tests/test_gemini.py with mocked requests
 ├── Author tests/test_cloudflare.py with mocked requests
 ├── Update tests/test_pipeline.py for new default chain
 └── Execute `pytest -v` and `python -m unittest discover -s tests -t .`

Step 6: Documentation & Review
 ├── Update README.md with new engines and env variables
 └── Review diff against core invariants
```

---

## 8. Documentation Changes (README.md)

1. **Engine Overview Table**:
   - Add `gemini` (Google Gemini with model switching: 3.5 Flash Lite -> 3.1 Flash Lite -> Gemma 4 26B).
   - Add `cloudflare` (Cloudflare Workers AI `@cf/meta/m2m100-1.2b`).
   - Move `mymemory` to tertiary position.
   - Mark `google_web` and `google_gtx` as legacy scrapers disabled by default.
2. **Environment Variables Table**:
   - Document `GEMINI_API_KEY`, `GEMINI_MODELS`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `MYMEMORY_EMAIL`.
3. **Pacing & Limits**:
   - Note the 4-5 second pause between Gemini requests to ensure sustainable free-tier usage.
4. **Tag & Numeral Invariants**:
   - Explain how CAT `{0}` tags and ASCII numerals are enforced.

---

## 9. Git Commit Specification

When the Engineer and Tester complete implementation and verification, use the following conventional commit message:

```text
feat(engine): add gemini and cloudflare engines with model fallback and validation

- Implement Gemini engine with model cascade (3.5 Flash Lite -> 3.1 Flash Lite -> Gemma 4 26B)
- Implement numbered JSON segment processing with thought token filtration and ASCII 0-9 enforcement
- Add tag preservation guardrails to fail segments that lose {0} placeholders
- Implement Cloudflare Workers AI engine (@cf/meta/m2m100-1.2b) as secondary fallback
- Update MyMemory engine with optional MYMEMORY_EMAIL quota configuration
- Update default fallback chain to gemini -> cloudflare -> mymemory
- Disable Google scraping engines (google_web, google_gtx) by default
- Add 4-5 second pacing delay for Gemini API calls
- Add comprehensive pytest suite with 100% mocked external API calls
```
