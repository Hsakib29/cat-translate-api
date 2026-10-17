# CAT Translate API & Cloud Translation Worker

A high-reliability private translation service and asynchronous document translation worker for Computer-Assisted Translation (CAT) tools.

The system operates in two coordinated modes:
1. **Synchronous REST API**: Fast, single-queue translation API with a multi-engine fallback chain (`gemini` → `cloudflare` → `mymemory`) for live segment translation.
2. **Asynchronous Cloud Document Worker**: Autonomous background worker daemon that polls the Lovable Cloud API bridge, translates large document segment batches in safe controlled chunks, and reconstructs `.docx` files preserving tables, multi-paragraph cells, and bullet lists.

---

## System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Lovable Cloud Application                       │
│  - User uploads DOCX                                                   │
│  - Document segments extracted & stored in Supabase                    │
│  - API Bridge Routes: /api/public/worker/(health|claim|segments|file)  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                       x-worker-api-key Authentication
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   Render: CAT Translate API & Worker                   │
│                                                                        │
│   ┌───────────────────────────┐     ┌──────────────────────────────┐   │
│   │   FastAPI Web Service     │     │   Background Worker Daemon   │   │
│   │   - GET  /api/health      │     │   - Polls /claim (5s - 30s)  │   │
│   │   - GET  /api/engines     │     │   - Safe batching (10-20/req)│   │
│   │   - GET  /api/selftest    │     │   - Chunk retry & isolation  │   │
│   │   - POST /api/translate   │     │   - Reconstructs DOCX tables │   │
│   └─────────────┬─────────────┘     └──────────────┬───────────────┘   │
│                 │                                  │                   │
│                 └─────────────────┬────────────────┘                   │
│                                   ▼                                    │
│                     Translation Engine Pipeline                        │
│               gemini  ──(fallback)──>  cloudflare  ──>  mymemory        │
│          (model cascade)           (Workers AI)      (REST API)        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Core System Features & Invariants

- **Safe Controlled Batching**: Translation segments are processed in chunks of 10 to 20 segments maximum per request, preventing engine timeouts and oversized payload rejections.
- **Chunk Tracking & Failure Isolation**: If an upstream engine encounters an error or timeout during a chunk, the worker retries with backoff. If exhausted, it falls back to source text for that chunk and commits progress, ensuring the job never freezes or leaves segments hanging.
- **1-to-1 Row & Paragraph Alignment**: Responses are mapped back to their exact Supabase segment IDs and document `segment_index` positions, preventing index drift or phrase collisions.
- **Multi-Paragraph Table Cell & Bullet Preservation**: The document parser (`app/document.py`) iterates `cell.paragraphs` for every cell in `table.rows` (never flattening `cell.text` or skipping multi-paragraph cells). It updates paragraph runs in-place without corrupting bullet list properties (`w:numPr`) or paragraph styles.
- **Fallback Chain Continuity**: `gemini` (with 3-model cascade) → `cloudflare` → `mymemory`. If an engine fails, the pipeline transitions seamlessly to the next engine.
- **Circuit Breaker & Cooldowns**: Immediate trips on bot detection (`blocked`) or HTTP 429 (`rate_limited`). Strike-based trips (3 consecutive errors) trigger stepped backoff (5m → 15m → 60m).
- **Absolute Privacy**: Segment text is **never** printed to stdout or logged to files or error traces. Error messages are sanitized via `textutils.scrub()`.

---

## Engine Chain

| Engine | Type | Priority | Description & Required Keys |
|---|---|---|---|
| `gemini` | Google Gemini LLM | **1st** | Model cascade (`gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemma-4-26b-a4b-it`). Numbered JSON segments, thought-token filtering, tag & digit validation. Needs `GEMINI_API_KEY`. |
| `cloudflare` | Cloudflare Workers AI | **2nd** | `@cf/meta/m2m100-1.2b` supporting 100+ language pairs natively. Needs `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`. |
| `mymemory` | MyMemory API | **3rd** | Free translation memory API via `deep-translator`. Optional `MYMEMORY_EMAIL` boosts daily quota from 5k to 50k chars. |
| `google_web` | Google Scraper | *Disabled* | Legacy scraper; can be enabled in `ENGINE_ORDER` if needed. |
| `google_gtx` | Google JSON | *Disabled* | Legacy endpoint; can be enabled in `ENGINE_ORDER` if needed. |

---

## Background Worker & Lovable API Bridge

When `WORKER_AUTOSTART=true` or when running `python worker.py`, the worker connects to the Lovable application using the shared `WORKER_API_KEY`.

### Worker Job Lifecycle
1. **Poll & Claim**: Worker calls `POST /api/public/worker/claim` with `x-worker-api-key`. Atomically claims the oldest `queued` job.
2. **Download Original File**: Calls `GET /api/public/worker/file?path=...` to download the source `.docx` binary from storage.
3. **Fetch Pending Segments**: Calls `GET /api/public/worker/segments?job_id=...&limit=20` to fetch uncompleted segments ordered by `segment_index ASC`.
4. **Controlled Chunk Translation**: Segments are sliced into chunks of `CHUNK_SIZE` (default 10, max 20) and translated via `pipeline.run_batch()`.
5. **Commit Translated Segments**: Calls `POST /api/public/worker/segments` with `{ id, target_text, status: "translated" }`.
6. **DOCX Document Reconstruction**: `reconstruct_docx` parses the original `.docx` XML package and updates every body paragraph and table cell paragraph via `index_to_target` and `source_to_target`.
7. **Upload Translated Document**: Calls `POST /api/public/worker/file` with the reconstructed DOCX bytes.
8. **Finalize Job Status**: Calls `POST /api/public/worker/status` setting status to `completed` and registering `translated_file_path`.

---

## REST Endpoints

All authenticated endpoints require `Authorization: Bearer <API_TOKEN>`.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/` | `GET` | None | Service heartbeat check. Returns `{"status": "healthy"}`. |
| `/api/health` | `GET` | None | Public health check. Wakes sleeping Render instances. |
| `/api/engines` | `GET` | Bearer | Engine status (`ok`, `cooldown`, etc.), cooldown timers, and statistics. |
| `/api/selftest` | `GET` | Bearer | Runs test translations through all engines from the current server IP. |
| `/api/translate` | `POST` | Bearer | Translates a batch of text segments synchronously. |

### POST /api/translate Example

**Request**:
```json
{
  "sl": "en",
  "tl": "bn",
  "segments": ["Welcome to our platform.", "Click {0} to proceed."],
  "engines": ["gemini", "cloudflare"]
}
```

**Response**:
```json
{
  "results": [
    {
      "text": "আমাদের প্ল্যাটফর্মে স্বাগতম।",
      "error": null,
      "via": "gemini",
      "cached": false,
      "retryable": false,
      "attempts": []
    },
    {
      "text": "এগিয়ে যেতে {0} এ ক্লিক করুন।",
      "error": null,
      "via": "gemini",
      "cached": false,
      "retryable": false,
      "attempts": []
    }
  ],
  "meta": {
    "elapsed_ms": 1420,
    "queue_wait_ms": 0,
    "engines": ["gemini", "cloudflare"]
  }
}
```

---

## Environment Variables & Configuration

Configure these in your `.env` file or in your hosting provider's dashboard (e.g., Render Environment Settings):

### Core API & Security
| Variable | Default | Description |
|---|---|---|
| `API_TOKEN` | *(required)* | Secure random token required for `/api/*` REST endpoints. |
| `ALLOWED_ORIGINS` | `""` | Comma-separated CORS origins (e.g. `https://cat-brandbounce.lovable.app`). |
| `ALLOW_LOCALHOST` | `false` | When `true`, permits localhost origins for local frontend testing. |
| `PORT` | `8000` | HTTP port for the web service. |

### Background Worker & Lovable Bridge
| Variable | Default | Description |
|---|---|---|
| `WORKER_API_KEY` | *(required for worker)* | Shared secret key for the Lovable API bridge (`x-worker-api-key`). |
| `LOVABLE_BASE_URL` | `https://cat-brandbounce.lovable.app/api/public/worker` | Base URL of Lovable worker endpoints. |
| `WORKER_AUTOSTART` | `false` | When `true`, automatically starts the worker thread inside the web container. |
| `BATCH_SIZE` | `20` | Maximum pending segments fetched per polling cycle (1 - 100). |
| `CHUNK_SIZE` | `10` | Maximum segments processed per translation pipeline request (1 - 20). |
| `POLL_INTERVAL` | `5.0` | Initial polling interval in seconds when checking for new jobs. |
| `IDLE_BACKOFF_MAX` | `30.0` | Maximum sleep duration in seconds when the job queue is empty. |
| `MAX_CHUNK_RETRIES` | `2` | Number of retries before falling back to source text for a chunk. |
| `WORKER_HTTP_TIMEOUT` | `60.0` | Timeout in seconds for HTTP requests to Lovable. |

### Translation Engines
| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(required for gemini)* | Google AI Studio API key. |
| `GEMINI_MODELS` | `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-26b-a4b-it` | Ordered cascade of Gemini models. |
| `CLOUDFLARE_ACCOUNT_ID` | `""` | Cloudflare account ID for Workers AI. |
| `CLOUDFLARE_API_TOKEN` | `""` | Cloudflare API token with `Workers AI:Run` permission. |
| `CLOUDFLARE_MODEL` | `@cf/meta/m2m100-1.2b` | Cloudflare translation model. |
| `MYMEMORY_EMAIL` | `""` | Email address to lift MyMemory daily quota to 50k chars. |
| `ENGINE_ORDER` | `gemini,cloudflare,mymemory` | Fallback chain execution order. |
| `ENGINE_DELAYS` | `gemini:4.0-5.0,cloudflare:0.5-1.0,mymemory:0.5-1.0` | Request pacing range per engine (seconds). |
| `COOLDOWN_STEPS` | `300,900,3600` | Progressive cooldown durations in seconds (5m → 15m → 60m). |

---

## Local Setup & Development

### 1. Create Virtual Environment & Install Dependencies
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Configure Local Environment
Create `.env`:
```ini
API_TOKEN=local-testing-secret-token-32chars
WORKER_API_KEY=your-lovable-worker-secret-key
LOVABLE_BASE_URL=https://cat-brandbounce.lovable.app/api/public/worker
WORKER_AUTOSTART=true
GEMINI_API_KEY=your-gemini-api-key
BATCH_SIZE=20
CHUNK_SIZE=10
```

### 3. Run the Service
**Run Web API with In-Process Worker Thread**:
```powershell
uvicorn app.main:create_app --factory --port 8000
```

**Or Run Standalone Background Worker**:
```powershell
python worker.py
```

### 4. Run Test Suite
```powershell
python -m unittest discover -s tests -t .
```

---

## Deployment on Render

1. **Create Web Service**:
   - In Render, create a new **Web Service** pointing to your repository.
   - Environment: **Python 3** (or **Docker**).
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT`

2. **Configure Environment Variables in Render**:
   - `API_TOKEN`: Random 32+ character string.
   - `WORKER_API_KEY`: Secret key matching `WORKER_API_KEY` in Lovable Project Settings.
   - `LOVABLE_BASE_URL`: `https://<your-project>.lovable.app/api/public/worker`
   - `WORKER_AUTOSTART`: `true` (enables worker thread alongside web server).
   - `GEMINI_API_KEY`: Google AI Studio API key.
   - `ALLOWED_ORIGINS`: `https://<your-project>.lovable.app`

3. **Verify Deployment**:
   - Visit `https://<your-service>.onrender.com/api/health` → should return `{"ok": true}`.
   - Trigger a translation job in Lovable → worker claims, processes in chunks, and uploads the translated DOCX file.
