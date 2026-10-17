"""Unit tests for the Lovable background worker (app/worker.py).
Tests all HTTP client interactions, DOCX reconstruction, and worker execution
using mocked responses (zero real network calls).
"""
import io
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import httpx

from app.worker import (
    JobProcessor,
    LovableWorkerClient,
    WorkerConfig,
    WorkerDaemon,
    reconstruct_docx,
)
from tests.helpers import FakeEngine, make_pipeline, make_settings

SAMPLE_DOCX_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
    <w:body>
        <w:p>
            <w:r><w:t>Hello world</w:t></w:r>
        </w:p>
        <w:p>
            <w:r><w:t>Second paragraph</w:t></w:r>
        </w:p>
    </w:body>
</w:document>"""


def make_dummy_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", SAMPLE_DOCX_XML)
        z.writestr("[Content_Types].xml", b"<Types></Types>")
    return buf.getvalue()


def fake_resp(status_code=200, **kwargs) -> httpx.Response:
    req = httpx.Request("GET", "https://app.example.com")
    return httpx.Response(status_code, request=req, **kwargs)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = WorkerConfig(
            lovable_base_url="https://app.example.com/api/public/worker",
            worker_api_key="test-secret-key-12345",
            poll_interval=1.0,
            batch_size=10,
        )

    def test_config_from_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(WorkerConfig.from_env())

        with patch.dict(
            "os.environ",
            {
                "WORKER_API_KEY": "my-key",
                "LOVABLE_BASE_URL": "https://custom.app/api/public/worker/",
                "POLL_INTERVAL": "2.5",
                "BATCH_SIZE": "15",
            },
            clear=True,
        ):
            cfg = WorkerConfig.from_env()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.worker_api_key, "my-key")
            self.assertEqual(cfg.lovable_base_url, "https://custom.app/api/public/worker")
            self.assertEqual(cfg.poll_interval, 2.5)
            self.assertEqual(cfg.batch_size, 15)

    def test_client_headers_and_auth(self):
        client = LovableWorkerClient(self.config)
        self.assertEqual(client.client.headers["x-worker-api-key"], "test-secret-key-12345")
        self.assertIn("CatTranslateWorker", client.client.headers["user-agent"])
        client.close()

    def test_client_health_check(self):
        mock_http = MagicMock()
        mock_http.get.return_value = fake_resp(200, json={"ok": True})
        client = LovableWorkerClient(self.config, client=mock_http)

        self.assertTrue(client.check_health())
        mock_http.get.assert_called_once_with("/health")

        mock_http.get.return_value = fake_resp(401, json={"error": "Unauthorized"})
        self.assertFalse(client.check_health())

    def test_client_claim_job(self):
        mock_http = MagicMock()
        mock_http.post.return_value = fake_resp(
            200,
            json={
                "job": {
                    "id": "job-uuid-1",
                    "user_id": "user-uuid-1",
                    "file_name": "test.docx",
                    "original_file_path": "user/file.docx",
                    "total_segments": 5,
                }
            },
        )
        client = LovableWorkerClient(self.config, client=mock_http)
        job = client.claim_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], "job-uuid-1")

        # Empty queue
        mock_http.post.return_value = fake_resp(200, json={"job": None})
        self.assertIsNone(client.claim_job())

        # 401 rejected
        mock_http.post.return_value = fake_resp(401, json={"error": "Unauthorized"})
        self.assertIsNone(client.claim_job())

    def test_client_segments_and_submission(self):
        mock_http = MagicMock()
        mock_http.get.return_value = fake_resp(
            200,
            json={"segments": [{"id": "s1", "segment_index": 0, "source_text": "Hello"}]},
        )
        mock_http.post.return_value = fake_resp(200, json={"ok": True, "completed_segments": 1})

        client = LovableWorkerClient(self.config, client=mock_http)
        segs = client.get_pending_segments("job-1", limit=10)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["id"], "s1")
        mock_http.get.assert_called_once_with("/segments", params={"job_id": "job-1", "limit": 10})

        res = client.submit_translated_segments("job-1", [{"id": "s1", "target_text": "হ্যালো"}])
        self.assertTrue(res["ok"])
        mock_http.post.assert_called_once_with(
            "/segments", json={"job_id": "job-1", "results": [{"id": "s1", "target_text": "হ্যালো"}]}
        )

    def test_reconstruct_docx(self):
        orig_bytes = make_dummy_docx()
        translations = {
            "Hello world": "হ্যালো বিশ্ব",
            "Second paragraph": "দ্বিতীয় অনুচ্ছেদ",
        }
        reconstructed = reconstruct_docx(orig_bytes, translations)
        self.assertGreater(len(reconstructed), 0)

        # Inspect resulting zip
        with zipfile.ZipFile(io.BytesIO(reconstructed), "r") as z:
            xml = z.read("word/document.xml").decode("utf-8")
            self.assertIn("হ্যালো বিশ্ব", xml)
            self.assertIn("দ্বিতীয় অনুচ্ছেদ", xml)
            self.assertNotIn("Hello world", xml)

    def test_job_processor_full_success_flow(self):
        dummy_docx = make_dummy_docx()
        mock_http = MagicMock()

        # Step 1: download_source_file
        mock_http.get.side_effect = [
            fake_resp(200, content=dummy_docx),  # /file
            fake_resp(  # /segments batch 1
                200,
                json={
                    "segments": [
                        {"id": "seg-1", "segment_index": 0, "source_text": "Hello world"},
                        {"id": "seg-2", "segment_index": 1, "source_text": "Second paragraph"},
                    ]
                },
            ),
            fake_resp(200, json={"segments": []}),  # /segments batch 2 (empty -> stop)
        ]

        # Step 2: submit_segments, upload_file, update_status
        mock_http.post.side_effect = [
            fake_resp(200, json={"ok": True, "completed_segments": 2}),  # /segments
            fake_resp(200, json={"ok": True, "translated_file_path": "user/job-1-translated.docx"}),  # /file
            fake_resp(200, json={"ok": True}),  # /status
        ]

        client = LovableWorkerClient(self.config, client=mock_http)
        engine = FakeEngine("test-engine", script=["হ্যালো বিশ্ব", "দ্বিতীয় অনুচ্ছেদ"])
        pipeline, _, _ = make_pipeline([engine])
        pipeline.s = make_settings(["test-engine"])

        processor = JobProcessor(client, pipeline, batch_size=10)
        job = {
            "id": "job-1",
            "user_id": "user-1",
            "file_name": "sample.docx",
            "original_file_path": "user/sample.docx",
            "total_segments": 2,
        }

        processor.process(job)

        # Verify all steps were called
        self.assertEqual(mock_http.post.call_count, 3)
        # Verify status update was completed with translated file path
        status_call = mock_http.post.call_args_list[-1]
        self.assertEqual(status_call[0][0], "/status")
        self.assertEqual(status_call[1]["json"]["status"], "completed")
        self.assertEqual(status_call[1]["json"]["translated_file_path"], "user/job-1-translated.docx")

    def test_job_processor_handles_failure(self):
        mock_http = MagicMock()
        mock_http.get.side_effect = Exception("Storage connection error")
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        engine = FakeEngine("test-engine")
        pipeline, _, _ = make_pipeline([engine])

        processor = JobProcessor(client, pipeline)
        job = {
            "id": "job-failed-1",
            "user_id": "user-1",
            "file_name": "sample.docx",
            "original_file_path": "user/sample.docx",
            "total_segments": 1,
        }

        # Should not raise exception, should report failed
        processor.process(job)

        mock_http.post.assert_called_once()
        status_call = mock_http.post.call_args_list[0]
        self.assertEqual(status_call[0][0], "/status")
        self.assertEqual(status_call[1]["json"]["status"], "failed")
        self.assertIn("Storage connection error", status_call[1]["json"]["error_message"])

    def test_daemon_run_once(self):
        mock_http = MagicMock()
        mock_http.post.return_value = fake_resp(200, json={"job": None})

        client = LovableWorkerClient(self.config, client=mock_http)
        engine = FakeEngine("test-engine")
        pipeline, _, _ = make_pipeline([engine])

        daemon = WorkerDaemon(self.config, pipeline, client=mock_http)
        found = daemon.run_once()
        self.assertFalse(found)


if __name__ == "__main__":
    unittest.main()
