# CAT Translate API

A small private translation service for your CAT tool. It takes text segments, translates them one request at a time through a chain of engines, and tells you which engine answered. It is stateless: glossary, memory and placeholders stay in the CAT tool.

## How it behaves

- **One request at a time.** A single queue handles every call, with a short pause between calls to the same engine. Run it with one worker (the Dockerfile does).
- **Fallback chain.** Default order: `gemini` (Google Gemini LLM with model cascade), then `cloudflare` (Cloudflare Workers AI), then `mymemory`. If one fails, the next is tried. Each result says which engine answered (`via`).
- **Cooldown.** A block page or HTTP 429 rests an engine at once. Other failures rest it after 3 in a row. Rests last 5 minutes, then 15, then 60, and a success resets that.
- **Never freezes.** Every engine call has a timeout, and a request that runs too long hands the remaining segments back with `retryable: true`.
- **Honest errors.** A failed segment comes back with the reason for every engine tried. A failed segment never fails the whole request.
- **Private.** Segment text is never written to logs or error messages. It is sent to the engine providers (Google, Cloudflare, MyMemory), so tell clients with confidentiality terms.
- **Tag & numeral preservation.** CAT placeholders like `{0}` and `{name}` are validated after every translation — a segment that loses a tag is rejected and retried on the next engine. ASCII digits `0-9` are always normalized back from any localized numerals the model may emit.

## Engine chain

| Engine | What it is | Default | Notes |
|---|---|---|---|
| `gemini` | Google Gemini LLM (`gemini-3.5-flash-lite` → `3.1-flash-lite` → `gemma-4-26b`) | **1st** | Numbered JSON segments, thought-token filtration, 4–5 s pacing. Needs `GEMINI_API_KEY`. |
| `cloudflare` | Cloudflare Workers AI (`@cf/meta/m2m100-1.2b`) | **2nd** | 100+ languages natively. Needs `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`. |
| `mymemory` | MyMemory free API via `deep-translator` | **3rd** | 5 k chars/day free; set `MYMEMORY_EMAIL` to lift to 50 k. Long text auto-chunked. |
| `google_web` | Google Translate via `deep-translator` | Disabled | Legacy scraper. Add to `ENGINE_ORDER` to re-enable. |
| `google_gtx` | Google's JSON endpoint | Disabled | Legacy scraper; was easily blocked. Add to `ENGINE_ORDER` to re-enable. |

### Gemini model cascade

Inside the `gemini` engine, models are tried in order:

1. `gemini-3.5-flash-lite` (primary)
2. `gemini-3.1-flash-lite` (secondary)
3. `gemma-4-26b-a4b-it` (tertiary)

If **all three** fail, the pipeline falls through to `cloudflare`, then `mymemory`.

## Endpoints

All except `/api/health` need `Authorization: Bearer <API_TOKEN>`.

| Endpoint | What it does |
|---|---|
| `GET /api/health` | Is the service awake? No token needed, so it also wakes a sleeping Render instance. |
| `GET /api/engines` | Each engine's status (`ok`, `failing`, `cooldown`, `untested`), last error, cooldown left, counters, and settings. |
| `GET /api/selftest?sl=en&tl=bn` | Sends two short test sentences through every engine and reports which work from this server's address and whether `{0}` placeholders survive. Limited to once a minute. Add `&force=true` to test an engine that is resting. |
| `POST /api/translate` | Translate up to 10 segments. |

Request:

```json
{ "sl": "en", "tl": "bn", "segments": ["Hello, how are you?", "Open {0} now."], "engines": ["gemini", "mymemory"] }
```

`engines` is optional; leave it out to use the default order.

Response:

```json
{
  "results": [
    { "text": "...", "error": null, "via": "gemini", "cached": false, "retryable": false, "attempts": [] },
    { "text": null, "error": "gemini: ...; cloudflare: ...", "via": null, "cached": false, "retryable": true,
      "attempts": [ { "engine": "gemini", "kind": "rate_limited", "message": "..." } ] }
  ],
  "meta": { "elapsed_ms": 4820, "queue_wait_ms": 0, "engines": ["gemini", "cloudflare", "mymemory"] }
}
```

- `via` is an engine name, `"passthrough"` for text with no letters (numbers, punctuation), or the engine that first produced a cached answer.
- `retryable: true` means trying again later may work (network, block, time limit). `false` means it won't (unsupported language pair, refused input).
- Error kinds: `blocked`, `rate_limited`, `network`, `empty`, `error`, `unsupported`, `bad_request`, `cooldown`, `deadline`.
- HTTP codes: 401 bad token, 400 bad input, 413 too big, 429 self-test too soon, 503 busy (retry after a few seconds).

## Run it on your computer (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# Copy .env.example to .env and fill in at minimum API_TOKEN and GEMINI_API_KEY
copy .env.example .env

$env:API_TOKEN = "any-long-secret-at-least-16-chars"
uvicorn app.main:create_app --factory --port 8000
```

In a second window:

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$h = @{ Authorization = "Bearer any-long-secret-at-least-16-chars" }
Invoke-RestMethod http://localhost:8000/api/health
Invoke-RestMethod http://localhost:8000/api/selftest -Headers $h | ConvertTo-Json -Depth 6
Invoke-RestMethod http://localhost:8000/api/translate -Method Post -Headers $h -ContentType "application/json; charset=utf-8" `
  -Body '{"sl":"en","tl":"bn","segments":["Hello, how are you?"]}' | ConvertTo-Json -Depth 6
```

Run the tests: `python -m pytest tests/ -v` (or `python -m unittest discover -s tests -t .`). The HTTP tests in `tests/test_api.py` only run when `fastapi` and `httpx` are installed (`requirements-dev.txt`).

## Deploy on Render

1. Put this folder in a **new private GitHub repository** (for example `cat-translate-api`). The `.env` file is ignored; never commit it.
2. Make a token. In PowerShell:
   ```powershell
   $b = New-Object byte[] 32; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($b); [Convert]::ToBase64String($b)
   ```
   Save it somewhere safe. You will paste it into the CAT tool later.
3. On Render: **New > Web Service**, connect the repository, choose **Docker** as the runtime and the **Free** plan. (Or use **New > Blueprint**, which reads `render.yaml`.)
4. Under **Environment**, add at minimum:
   - `API_TOKEN` = the token from step 2
   - `GEMINI_API_KEY` = your Google AI Studio API key (required for the primary engine)
   - `ALLOWED_ORIGINS` = your CAT site addresses, comma separated, e.g. `https://your-site.lovable.app,https://*.lovable.app`
   - Optionally: `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `MYMEMORY_EMAIL`
5. Set the health check path to `/api/health`, then deploy.
6. When it is live, check, in this order:
   - `GET /api/health` shows `"ok": true`
   - `GET /api/selftest` with your token. This tells you which engines work **from Render's address**. Send me the result if any show `failed` or `cooldown`.
   - one small `POST /api/translate`
7. To change the token: set a new `API_TOKEN` in Render, redeploy, and update the CAT tool.

Things to expect on the free plan: the service sleeps after a period of no traffic, so the first request can take up to a minute (call `/api/health` first). The cache and cooldown state reset when it restarts or sleeps.

## Settings (environment variables)

Everything is an environment variable; `.env.example` lists them all with explanations. The ones you are most likely to change:

| Variable | Default | Purpose |
|---|---|---|
| `API_TOKEN` | *(required)* | Long random bearer token for all authenticated endpoints. |
| `GEMINI_API_KEY` | *(required for gemini)* | Single Google AI Studio API key. No key rotation. |
| `GEMINI_MODELS` | `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-26b-a4b-it` | Ordered model cascade inside the Gemini engine. |
| `CLOUDFLARE_ACCOUNT_ID` | *(optional)* | Cloudflare account ID for Workers AI. |
| `CLOUDFLARE_API_TOKEN` | *(optional)* | Cloudflare API token with Workers AI:Run permission. |
| `CLOUDFLARE_MODEL` | `@cf/meta/m2m100-1.2b` | Cloudflare translation model. |
| `MYMEMORY_EMAIL` | *(optional)* | Email for MyMemory free quota (5 k → 50 k chars/day). |
| `ENGINE_ORDER` | `gemini,cloudflare,mymemory` | Fallback chain order. |
| `ENGINE_DELAYS` | `gemini:4.0-5.0,cloudflare:0.5-1.0,mymemory:0.5-1.0` | Per-engine pacing (seconds). |
| `COOLDOWN_STEPS` | `300,900,3600` | Engine cooldown escalation (5 min → 15 min → 60 min). |
| `ALLOWED_ORIGINS` | *(empty)* | CORS origins for browser clients, comma separated. |

## Troubleshooting

| What you see | What it means |
|---|---|
| `401 Invalid or missing token` | The token in the CAT tool differs from `API_TOKEN`, or the header is not `Authorization: Bearer ...`. |
| Browser says CORS blocked | The site's address is not in `ALLOWED_ORIGINS`. Add it exactly as the browser shows it (scheme included, no trailing slash). |
| Every engine shows `blocked` or `cooldown` | An upstream provider is refusing this server's address. Wait for the cooldown, or check API key validity. |
| `engine ... cooling down` in `attempts` | That engine is resting on purpose. `/api/engines` shows how long is left. |
| `503 Busy` | Another request is running. The CAT tool should wait a few seconds and retry. |
| First request very slow | Render woke the service up. Call `/api/health` first. |
| Gemini returns localized digits (০-৯) | The normalizer converts them to ASCII 0-9 automatically; if you still see them, check `textutils.normalize_ascii_digits`. |

## Adding another engine later

1. Create `app/engines/my_engine.py` with a class that has a `name` and a `translate(text, sl, tl, timeout)` method. It returns text or raises `EngineError(kind, message)`. Use `classify_exception` for library errors.
2. Add the class to `ENGINE_CLASSES` in `app/engines/__init__.py`.
3. Put its name in `ENGINE_ORDER` and, if it needs a different pause, in `ENGINE_DELAYS`.
4. Add keys for official services (Azure, Google Cloud) as environment variables in Render, never in the code.

Official engines will also need a monthly character cap that survives restarts. That needs a small database, which is left for a later phase.

## What has and has not been tested

The queue, fallback chain, cooldowns, pacing, timeouts, cache, validation and error scrubbing have unit tests with fake engines. The Gemini engine is tested with mocked `requests.post` responses (including thought-token filtration, model cascade, tag preservation, digit normalization, and 429 cascades). The Cloudflare and MyMemory engines are tested with mocked HTTP responses. The API layer is tested with FastAPI's test client. Nothing here has been run against the real Gemini or Cloudflare services in CI, so the `/api/selftest` endpoint is the real integration check.
