# CAT Translate API

A small private translation service for your CAT tool. It takes text segments, translates them one request at a time through a chain of engines, and tells you which engine answered. It is stateless: glossary, memory and placeholders stay in the CAT tool.

## How it behaves

- **One request at a time.** A single queue handles every call, with a short pause between calls to the same engine. Run it with one worker (the Dockerfile does).
- **Fallback chain.** Default order: `google_web`, then `mymemory`, then `google_gtx`. If one fails, the next is tried. Each result says which engine answered (`via`).
- **Cooldown.** A block page or HTTP 429 rests an engine at once. Other failures rest it after 3 in a row. Rests last 5 minutes, then 15, then 60, and a success resets that.
- **Never freezes.** Every engine call has a timeout, and a request that runs too long hands the remaining segments back with `retryable: true`.
- **Honest errors.** A failed segment comes back with the reason for every engine tried. A failed segment never fails the whole request.
- **Private.** Segment text is never written to logs or error messages. It is sent to the engine providers (Google, MyMemory), so tell clients with confidentiality terms.

| Engine | What it is | Notes |
|---|---|---|
| `google_web` | Google Translate through the `deep-translator` library | The one your Render backend uses today |
| `mymemory` | MyMemory free API through `deep-translator` | Free daily limit and long text is sent in small pieces; check its current terms |
| `google_gtx` | Google's JSON endpoint | The one the CAT site used; kept last because it was blocked |

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
{ "sl": "en", "tl": "bn", "segments": ["Hello, how are you?", "Open {0} now."], "engines": ["google_web", "mymemory"] }
```

`engines` is optional; leave it out to use the default order.

Response:

```json
{
  "results": [
    { "text": "...", "error": null, "via": "google_web", "cached": false, "retryable": false, "attempts": [] },
    { "text": null, "error": "google_web: ...; mymemory: ...", "via": null, "cached": false, "retryable": true,
      "attempts": [ { "engine": "google_web", "kind": "blocked", "message": "..." } ] }
  ],
  "meta": { "elapsed_ms": 2310, "queue_wait_ms": 0, "engines": ["google_web", "mymemory", "google_gtx"] }
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

Run the tests: `python -m unittest discover -s tests -t .` (or `pytest`). The HTTP tests in `tests/test_api.py` only run when `fastapi` and `httpx` are installed (`requirements-dev.txt`).

## Deploy on Render

1. Put this folder in a **new private GitHub repository** (for example `cat-translate-api`). The `.env` file is ignored; never commit a real token.
2. Make a token. In PowerShell:
   ```powershell
   $b = New-Object byte[] 32; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($b); [Convert]::ToBase64String($b)
   ```
   Save it somewhere safe. You will paste it into the CAT tool later.
3. On Render: **New > Web Service**, connect the repository, choose **Docker** as the runtime and the **Free** plan. (Or use **New > Blueprint**, which reads `render.yaml`.)
4. Under **Environment**, add:
   - `API_TOKEN` = the token from step 2
   - `ALLOWED_ORIGINS` = your CAT site addresses, comma separated, for example `https://your-site.lovable.app,https://*.lovable.app,https://*.lovableproject.com`. Check the exact address in your browser's address bar, since preview addresses can differ.
5. Set the health check path to `/api/health`, then deploy.
6. When it is live, check, in this order:
   - `GET /api/health` shows `"ok": true`
   - `GET /api/selftest` with your token. This tells you which engines work **from Render's address**. Send me the result if any show `failed` or `cooldown`.
   - one small `POST /api/translate`
7. To change the token: set a new `API_TOKEN` in Render, redeploy, and update the CAT tool.

Things to expect on the free plan: the service sleeps after a period of no traffic, so the first request can take up to a minute (call `/api/health` first). The cache and cooldown state reset when it restarts or sleeps.

## Settings

Everything is an environment variable; `.env.example` lists them all with explanations. The ones you are most likely to change are `ENGINE_ORDER`, `ENGINE_DELAYS`, `ALLOWED_ORIGINS` and `COOLDOWN_STEPS`.

## Troubleshooting

| What you see | What it means |
|---|---|
| `401 Invalid or missing token` | The token in the CAT tool differs from `API_TOKEN`, or the header is not `Authorization: Bearer ...`. |
| Browser says CORS blocked | The site's address is not in `ALLOWED_ORIGINS`. Add it exactly as the browser shows it (scheme included, no trailing slash). |
| Every engine shows `blocked` or `cooldown` | Google is refusing this server's address. Wait for the cooldown, use fewer segments, or add an official engine later. |
| `engine ... cooling down` in `attempts` | That engine is resting on purpose. `/api/engines` shows how long is left. |
| `503 Busy` | Another request is running. The CAT tool should wait a few seconds and retry. |
| First request very slow | Render woke the service up. Call `/api/health` first. |

## Adding another engine later

1. Create `app/engines/my_engine.py` with a class that has a `name` and a `translate(text, sl, tl, timeout)` method. It returns text or raises `EngineError(kind, message)`. Use `classify_exception` for library errors.
2. Add the class to `ENGINE_CLASSES` in `app/engines/__init__.py`.
3. Put its name in `ENGINE_ORDER` and, if it needs a different pause, in `ENGINE_DELAYS`.
4. Add keys for official services (Azure, Google Cloud) as environment variables in Render, never in the code.

Official engines will also need a monthly character cap that survives restarts. That needs a small database, which is left for a later phase.

## What has and has not been tested

The queue, fallback chain, cooldowns, pacing, timeouts, cache, validation and error scrubbing have unit tests with fake engines. The engine classes are tested against stand-ins for `deep-translator` and `requests`. Nothing here has been run against the real Google or MyMemory services, or on Render itself, so the self-test above is the real check.
