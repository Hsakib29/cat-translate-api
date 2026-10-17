"""Background translation worker for Lovable Cloud.
Polls Lovable app server routes using httpx and WORKER_API_KEY.
Translates segment batches using the CAT translation pipeline and reconstructs DOCX documents.
"""
from __future__ import annotations

import io
import logging
import os
import signal
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx

from .config import Settings
from .engines import build_engines
from .pipeline import Pipeline

log = logging.getLogger("cat.worker")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True)
class WorkerConfig:
    lovable_base_url: str
    worker_api_key: str
    poll_interval: float = 5.0
    idle_backoff_max: float = 30.0
    batch_size: int = 20
    chunk_size: int = 5
    timeout_seconds: float = 60.0
    max_chunk_retries: int = 2
    supabase_url: str = ""
    supabase_key: str = ""

    @classmethod
    def from_env(cls) -> Optional[WorkerConfig]:
        api_key = os.environ.get("WORKER_API_KEY", "").strip()
        if not api_key:
            return None
        base_url = os.environ.get(
            "LOVABLE_BASE_URL", "https://cat-brandbounce.lovable.app/api/public/worker"
        ).strip().rstrip("/")
        poll_interval = float(os.environ.get("POLL_INTERVAL", "5.0"))
        batch_size = max(1, min(100, int(os.environ.get("BATCH_SIZE", "20"))))
        chunk_size = max(1, min(20, int(os.environ.get("CHUNK_SIZE", "5"))))
        max_chunk_retries = max(0, min(5, int(os.environ.get("MAX_CHUNK_RETRIES", "2"))))
        idle_backoff_max = float(os.environ.get("IDLE_BACKOFF_MAX", "30.0"))
        timeout = float(os.environ.get("WORKER_HTTP_TIMEOUT", "60.0"))
        supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        supabase_key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
        ).strip()

        return cls(
            lovable_base_url=base_url,
            worker_api_key=api_key,
            poll_interval=poll_interval,
            idle_backoff_max=idle_backoff_max,
            batch_size=batch_size,
            chunk_size=chunk_size,
            timeout_seconds=timeout,
            max_chunk_retries=max_chunk_retries,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )


class LovableWorkerClient:
    """HTTP client communicating with Lovable server routes."""

    def __init__(self, config: WorkerConfig, client: Optional[httpx.Client] = None):
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.lovable_base_url,
            headers={
                "x-worker-api-key": config.worker_api_key,
                "User-Agent": "CatTranslateWorker/2.0 (Render)",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
        )

    def close(self) -> None:
        self.client.close()

    def check_health(self) -> bool:
        """GET /health - Verifies connectivity and secret key."""
        try:
            resp = self.client.get("/health")
            return resp.status_code == 200 and resp.json().get("ok") is True
        except Exception as exc:
            log.warning("Worker health check failed: %s", exc)
            return False

    def claim_job(self) -> Optional[Dict[str, Any]]:
        """POST /claim - Atomically claims the oldest queued job."""
        try:
            resp = self.client.post("/claim", json={})
            if resp.status_code == 401:
                log.error("Authentication rejected: WORKER_API_KEY invalid.")
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("job")
        except httpx.HTTPError as exc:
            log.warning("HTTP error claiming job: %s", exc)
            return None

    def get_pending_segments(self, job_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """GET /segments?job_id=...&limit=... - Fetches pending segments."""
        resp = self.client.get("/segments", params={"job_id": job_id, "limit": limit})
        resp.raise_for_status()
        return resp.json().get("segments", [])

    def submit_translated_segments(
        self, job_id: str, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """POST /segments - Submits translated batch of segments."""
        resp = self.client.post("/segments", json={"job_id": job_id, "results": results})
        resp.raise_for_status()
        return resp.json()

    def download_source_file(self, original_path: str) -> bytes:
        """GET /file?path=... - Downloads original DOCX binary."""
        resp = self.client.get("/file", params={"path": original_path})
        resp.raise_for_status()
        return resp.content

    def get_glossary(
        self,
        job_id: str,
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Fetches custom glossary terms for the job's project.
        Checks GET /glossary on the Lovable server, returning a normalized dict {src: tgt}.
        """
        try:
            params: Dict[str, str] = {"job_id": job_id}
            if project_id:
                params["project_id"] = project_id
            if user_id:
                params["user_id"] = user_id
            resp = self.client.get("/glossary", params=params)
            if resp.status_code == 200:
                raw = resp.json().get("glossary", {})
                return self._parse_glossary_payload(raw)
        except Exception as exc:
            log.warning("Could not fetch glossary via API for Job %s: %s", job_id, exc)

        if self.config.supabase_url and self.config.supabase_key:
            return self._fetch_glossary_from_supabase(project_id, user_id)

        return {}

    @staticmethod
    def _parse_glossary_payload(raw: Any) -> Dict[str, str]:
        """Normalizes glossary payloads (list of dicts or key-value map) into {src: tgt}."""
        result: Dict[str, str] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, str) and k.strip() and v.strip():
                    result[k.strip()] = v.strip()
                elif isinstance(v, dict):
                    src = v.get("src") or v.get("term") or k
                    tgt = v.get("tgt") or v.get("translation")
                    if src and tgt:
                        result[str(src).strip()] = str(tgt).strip()
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    src = item.get("src") or item.get("term")
                    tgt = item.get("tgt") or item.get("translation")
                    if src and tgt and str(src).strip() and str(tgt).strip():
                        result[str(src).strip()] = str(tgt).strip()
        return result

    def _fetch_glossary_from_supabase(
        self, project_id: Optional[str], user_id: Optional[str]
    ) -> Dict[str, str]:
        """Direct fallback query to Supabase REST endpoint (cat_projects table)."""
        try:
            headers = {
                "apikey": self.config.supabase_key,
                "Authorization": f"Bearer {self.config.supabase_key}",
                "Accept": "application/json",
            }
            url = f"{self.config.supabase_url.rstrip('/')}/rest/v1/cat_projects"
            params: Dict[str, str] = {"select": "glossary,project"}
            if project_id:
                params["id"] = f"eq.{project_id}"
            elif user_id:
                params["user_id"] = f"eq.{user_id}"
            else:
                return {}

            resp = httpx.get(url, headers=headers, params=params, timeout=10.0)
            if resp.status_code == 200:
                rows = resp.json()
                if rows and isinstance(rows, list):
                    first = rows[0]
                    glossary_data = first.get("glossary") or first.get("project", {}).get("glossary")
                    if glossary_data:
                        return self._parse_glossary_payload(glossary_data)
        except Exception as exc:
            log.warning("Direct Supabase glossary query failed: %s", exc)
        return {}

    def upload_translated_file(
        self, user_id: str, job_id: str, filename: str, docx_bytes: bytes
    ) -> str:
        """POST /file?user_id=...&job_id=...&file_name=... - Uploads translated DOCX binary."""
        resp = self.client.post(
            "/file",
            params={"user_id": user_id, "job_id": job_id, "file_name": filename},
            content=docx_bytes,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
        )
        resp.raise_for_status()
        return resp.json().get("translated_file_path", "")

    def update_job_status(
        self,
        job_id: str,
        status: str,
        translated_file_path: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """POST /status - Updates final job status (completed or failed)."""
        payload: Dict[str, Any] = {"job_id": job_id, "status": status}
        if translated_file_path:
            payload["translated_file_path"] = translated_file_path
        if error_message:
            payload["error_message"] = error_message[:4500]

        resp = self.client.post("/status", json=payload)
        resp.raise_for_status()


import docx
from .doc_processor import extract_segments_from_docx, is_bilingual_table, reconstruct_docx
from .textutils import to_bn_digits
from .utils.glossary import protect_terms, restore_terms


class JobProcessor:
    """Processes a single translation job: fetches segments, translates in safe controlled chunks,
    updates database rows, and rebuilds file with clean 1-to-1 index mapping."""

    def __init__(
        self,
        api: LovableWorkerClient,
        pipeline: Pipeline,
        batch_size: int = 20,
        chunk_size: int = 5,
        max_chunk_retries: int = 2,
    ):
        self.api = api
        self.pipeline = pipeline
        self.batch_size = batch_size
        self.chunk_size = max(1, min(20, chunk_size))
        self.max_chunk_retries = max_chunk_retries

    def process(self, job: Dict[str, Any]) -> None:
        job_id = job["id"]
        user_id = job["user_id"]
        filename = job.get("file_name", "document.docx")
        orig_path = job["original_file_path"]
        total_segments = job.get("total_segments", 0)
        sl = job.get("source_language", "en")
        tl = job.get("target_language", "bn")
        is_bn = tl.lower() in ("bn", "ben", "bengali")

        log.info(
            "Processing Job ID=%s File=%s TotalSegments=%d (sl=%s, tl=%s, batch_size=%d, chunk_size=%d)",
            job_id, filename, total_segments, sl, tl, self.batch_size, self.chunk_size
        )

        # Retrieve and attach project custom glossary to job context
        raw_glossary = job.get("glossary")
        project_id = job.get("project_id")
        if raw_glossary:
            glossary = LovableWorkerClient._parse_glossary_payload(raw_glossary)
        elif project_id or (self.api.config.supabase_url and self.api.config.supabase_key):
            glossary = self.api.get_glossary(job_id=job_id, project_id=project_id, user_id=user_id)
        else:
            glossary = {}
        job["glossary"] = glossary
        if glossary:
            log.info("Job %s: Loaded %d custom glossary terms for translation.", job_id, len(glossary))

        source_to_target: Dict[str, str] = {}
        index_to_target: Dict[int, str] = {}
        processed_count = 0
        total_chunks = 0
        failed_chunks = 0

        try:
            # 1. Download original DOCX if present
            orig_bytes: Optional[bytes] = None
            if orig_path:
                try:
                    orig_bytes = self.api.download_source_file(orig_path)
                except Exception as dl_err:
                    log.warning("Could not download original file for Job %s: %s", job_id, dl_err)

            # 2. Iterate through pending segments in batches
            while True:
                pending = self.api.get_pending_segments(job_id, limit=self.batch_size)
                if not pending:
                    break

                # Partition into safe, controlled chunks of 5 segments maximum per request
                chunks = [pending[i:i + self.chunk_size] for i in range(0, len(pending), self.chunk_size)]

                for chunk in chunks:
                    total_chunks += 1

                    # Protect glossary terms before submitting chunk to pipeline
                    chunk_source_texts: List[str] = []
                    chunk_token_maps: List[Dict[str, str]] = []
                    for seg in chunk:
                        raw_src = seg.get("source_text", "")
                        if glossary:
                            prot_text, tok_map = protect_terms(raw_src, glossary)
                        else:
                            prot_text, tok_map = raw_src, {}
                        chunk_source_texts.append(prot_text)
                        chunk_token_maps.append(tok_map)

                    # Chunk translation with retry and error isolation
                    results = None
                    last_chunk_err = None
                    for attempt in range(self.max_chunk_retries + 1):
                        try:
                            results, _meta = self.pipeline.run_batch(chunk_source_texts, sl=sl, tl=tl)
                            break
                        except Exception as err:
                            last_chunk_err = err
                            log.warning(
                                "Job %s: Chunk %d (size=%d) translation attempt %d failed: %s",
                                job_id, total_chunks, len(chunk), attempt + 1, err
                            )
                            if attempt < self.max_chunk_retries:
                                time.sleep(1.0 * (attempt + 1))

                    # Clean response parsing: map strictly back to database rows and indices
                    chunk_submission = []
                    for idx, seg in enumerate(chunk):
                        res = results[idx] if (results and idx < len(results)) else None
                        tok_map = chunk_token_maps[idx] if idx < len(chunk_token_maps) else {}

                        if res and res.text is not None:
                            target_text = restore_terms(res.text, tok_map) if tok_map else res.text
                        else:
                            # Per-segment isolated fallback: if batch failed or segment was omitted,
                            # attempt single-segment translation with glossary protection
                            source_text = seg.get("source_text", "")
                            target_text = None
                            if len(chunk) > 1 and source_text.strip():
                                try:
                                    prot_single, single_map = (
                                        protect_terms(source_text, glossary) if glossary else (source_text, {})
                                    )
                                    single_res, _ = self.pipeline.run_batch([prot_single], sl=sl, tl=tl)
                                    if single_res and single_res[0].text is not None:
                                        target_text = (
                                            restore_terms(single_res[0].text, single_map)
                                            if single_map
                                            else single_res[0].text
                                        )
                                except Exception as seg_err:
                                    log.warning(
                                        "Job %s: Per-segment fallback for segment %s failed: %s",
                                        job_id, seg.get("id"), seg_err
                                    )
                            if target_text is None:
                                target_text = source_text

                        status = "translated"

                        # Apply digit localization for Bengali target language
                        if is_bn:
                            target_text = to_bn_digits(target_text)

                        chunk_submission.append({
                            "id": seg["id"],
                            "target_text": target_text,
                            "status": status,
                        })

                        clean_source = seg.get("source_text", "").strip()
                        if clean_source:
                            source_to_target[clean_source] = target_text
                        if "segment_index" in seg and seg["segment_index"] is not None:
                            try:
                                index_to_target[int(seg["segment_index"])] = target_text
                            except (ValueError, TypeError):
                                pass

                    if results is None:
                        failed_chunks += 1
                        log.warning(
                            "Job %s: Chunk %d batch attempt failed (%s); executed per-segment recovery.",
                            job_id, total_chunks, last_chunk_err
                        )

                    # Submit translated chunk to database with retry
                    for sub_attempt in range(3):
                        try:
                            self.api.submit_translated_segments(job_id, chunk_submission)
                            processed_count += len(chunk_submission)
                            break
                        except Exception as sub_err:
                            log.warning(
                                "Job %s: Chunk %d submission attempt %d failed: %s",
                                job_id, total_chunks, sub_attempt + 1, sub_err
                            )
                            if sub_attempt == 2:
                                raise
                            time.sleep(2.0 * (sub_attempt + 1))

                    log.info(
                        "Job %s: Committed chunk %d (%d segments) - Total processed: %d/%s",
                        job_id, total_chunks, len(chunk), processed_count, total_segments or "?"
                    )

            # 3. Rebuild and upload translated DOCX if original was retrieved
            translated_path: Optional[str] = None
            if orig_bytes:
                try:
                    # Read bilingual column config from job metadata (if present)
                    # Default: source_col=0 (C0=English), target_col=1 (C1=Translation)
                    bilingual_source_col: int = int(job.get("bilingual_source_col", 0))
                    bilingual_target_col: Optional[int] = job.get("bilingual_target_col")

                    # Auto-detect if not explicitly configured in job metadata
                    if bilingual_target_col is None:
                        try:
                            doc_preview = docx.Document(io.BytesIO(orig_bytes))
                            if any(is_bilingual_table(t, 0, 1) for t in doc_preview.tables):
                                bilingual_source_col = 0
                                bilingual_target_col = 1
                                log.info(
                                    "Job %s: Auto-detected two-column bilingual table layout (source_col=0, target_col=1).",
                                    job_id,
                                )
                        except Exception as detect_err:
                            log.warning("Job %s: Bilingual table auto-detection skipped: %s", job_id, detect_err)
                    elif bilingual_target_col is not None:
                        bilingual_target_col = int(bilingual_target_col)
                        log.info(
                            "Job %s: Bilingual mode — writing translations into col %d (source col %d).",
                            job_id, bilingual_target_col, bilingual_source_col,
                        )

                    reconstructed = reconstruct_docx(
                        orig_bytes,
                        source_to_target,
                        index_to_target=index_to_target,
                        bilingual_target_col=bilingual_target_col,
                        bilingual_source_col=bilingual_source_col,
                        tl=tl,
                    )
                    translated_path = self.api.upload_translated_file(
                        user_id=user_id,
                        job_id=job_id,
                        filename=filename,
                        docx_bytes=reconstructed,
                    )
                    log.info("Job %s: Reconstructed DOCX uploaded to: %s", job_id, translated_path)
                except Exception as up_err:
                    log.error("Job %s: Failed to reconstruct or upload DOCX: %s", job_id, up_err)

            # 4. Mark job completed
            self.api.update_job_status(job_id, status="completed", translated_file_path=translated_path)
            log.info(
                "Job %s successfully COMPLETED. (Total segments: %d, chunks: %d, recovered failures: %d)",
                job_id, processed_count, total_chunks, failed_chunks
            )

        except Exception as exc:
            log.exception("Error processing Job %s: %s", job_id, exc)
            try:
                self.api.update_job_status(job_id, status="failed", error_message=str(exc))
            except Exception as report_err:
                log.error("Failed to mark Job %s as failed: %s", job_id, report_err)


class WorkerDaemon:
    """Manages the background polling loop with adaptive backoff and graceful shutdown."""

    def __init__(self, config: WorkerConfig, pipeline: Pipeline, client: Optional[httpx.Client] = None):
        self.config = config
        self.api = LovableWorkerClient(config, client=client)
        self.processor = JobProcessor(
            self.api,
            pipeline,
            batch_size=config.batch_size,
            chunk_size=config.chunk_size,
            max_chunk_retries=config.max_chunk_retries,
        )
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False

    def run_once(self) -> bool:
        """Polls once for a job. Returns True if a job was found and processed."""
        job = self.api.claim_job()
        if job:
            self.processor.process(job)
            return True
        return False

    def run(self) -> None:
        log.info(
            "Worker started. Polling %s every %.1fs (batch_size=%d)...",
            self.config.lovable_base_url,
            self.config.poll_interval,
            self.config.batch_size,
        )
        current_sleep = self.config.poll_interval

        try:
            while self.is_running:
                found = self.run_once()
                if found:
                    current_sleep = self.config.poll_interval
                else:
                    time.sleep(current_sleep)
                    current_sleep = min(current_sleep * 1.25, self.config.idle_backoff_max)
        finally:
            self.api.close()
            log.info("Worker stopped.")


def main() -> None:
    config = WorkerConfig.from_env()
    if not config:
        log.error("FATAL: WORKER_API_KEY environment variable is required.")
        sys.exit(1)

    settings = Settings.from_env()
    engines = build_engines(settings)
    pipeline = Pipeline(engines, settings)

    daemon = WorkerDaemon(config, pipeline)

    def handle_signal(sig: int, _frame: Any) -> None:
        log.info("Received signal %d. Shutting down worker gracefully...", sig)
        daemon.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    daemon.run()


if __name__ == "__main__":
    main()
