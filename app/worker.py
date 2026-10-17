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
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> Optional[WorkerConfig]:
        api_key = os.environ.get("WORKER_API_KEY", "").strip()
        if not api_key:
            return None
        base_url = os.environ.get(
            "LOVABLE_BASE_URL", "https://cat-brandbounce.lovable.app/api/public/worker"
        ).strip().rstrip("/")
        poll_interval = float(os.environ.get("POLL_INTERVAL", "5.0"))
        batch_size = max(1, min(500, int(os.environ.get("BATCH_SIZE", "20"))))
        idle_backoff_max = float(os.environ.get("IDLE_BACKOFF_MAX", "30.0"))
        timeout = float(os.environ.get("WORKER_HTTP_TIMEOUT", "60.0"))

        return cls(
            lovable_base_url=base_url,
            worker_api_key=api_key,
            poll_interval=poll_interval,
            idle_backoff_max=idle_backoff_max,
            batch_size=batch_size,
            timeout_seconds=timeout,
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


from .document import extract_segments_from_docx, reconstruct_docx


class JobProcessor:
    """Processes a single translation job: fetches segments, translates, updates, and rebuilds file."""

    def __init__(self, api: LovableWorkerClient, pipeline: Pipeline, batch_size: int = 20):
        self.api = api
        self.pipeline = pipeline
        self.batch_size = batch_size

    def process(self, job: Dict[str, Any]) -> None:
        job_id = job["id"]
        user_id = job["user_id"]
        filename = job.get("file_name", "document.docx")
        orig_path = job["original_file_path"]
        total_segments = job.get("total_segments", 0)

        log.info("Processing Job ID=%s File=%s TotalSegments=%d", job_id, filename, total_segments)

        source_to_target: Dict[str, str] = {}
        processed_count = 0

        try:
            # 1. Download original DOCX if present
            orig_bytes: Optional[bytes] = None
            try:
                orig_bytes = self.api.download_source_file(orig_path)
            except Exception as dl_err:
                log.warning("Could not download original file for Job %s: %s", job_id, dl_err)

            # 2. Iterate through pending segments in batches
            while True:
                pending = self.api.get_pending_segments(job_id, limit=self.batch_size)
                if not pending:
                    break

                source_texts = [seg["source_text"] for seg in pending]
                results, _meta = self.pipeline.run_batch(source_texts, sl="en", tl="bn")

                batch_submission = []
                for seg, res in zip(pending, results):
                    # Segment isolation: if engine failed, fallback to source text so job can proceed
                    target_text = res.text if res.text is not None else seg["source_text"]
                    batch_submission.append({
                        "id": seg["id"],
                        "target_text": target_text,
                        "status": "translated" if res.text is not None else "pending",
                    })
                    source_to_target[seg["source_text"].strip()] = target_text

                self.api.submit_translated_segments(job_id, batch_submission)
                processed_count += len(batch_submission)
                log.info(
                    "Job %s: Committed batch of %d segments (Total processed: %d)",
                    job_id, len(batch_submission), processed_count
                )

            # 3. Rebuild and upload translated DOCX if original was retrieved
            translated_path: Optional[str] = None
            if orig_bytes:
                try:
                    reconstructed = reconstruct_docx(orig_bytes, source_to_target)
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
            log.info("Job %s successfully COMPLETED.", job_id)

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
        self.processor = JobProcessor(self.api, pipeline, batch_size=config.batch_size)
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
