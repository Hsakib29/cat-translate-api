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

    def test_job_processor_controlled_chunking(self):
        mock_http = MagicMock()
        # 25 pending segments
        pending_25 = [
            {"id": f"seg-{i}", "segment_index": i, "source_text": f"Sentence {i}"}
            for i in range(25)
        ]
        mock_http.get.side_effect = [
            fake_resp(200, json={"segments": pending_25}),
            fake_resp(200, json={"segments": []}),
        ]
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        mock_pipeline = MagicMock()
        # Track lengths of batches passed to run_batch
        passed_batch_sizes = []

        def fake_run_batch(texts, sl="en", tl="bn"):
            passed_batch_sizes.append(len(texts))
            from app.pipeline import Result
            return [Result(text=f"Trans: {t}") for t in texts], {}

        mock_pipeline.run_batch.side_effect = fake_run_batch

        # Initialize processor with batch_size=25, chunk_size=10
        processor = JobProcessor(client, mock_pipeline, batch_size=25, chunk_size=10)
        job = {
            "id": "job-chunk-1",
            "user_id": "user-1",
            "file_name": "test.docx",
            "original_file_path": "",
            "total_segments": 25,
        }

        processor.process(job)

        # Chunks must be [10, 10, 5], never oversized
        self.assertEqual(passed_batch_sizes, [10, 10, 5])
        # Verify 3 chunk submissions to /segments
        submission_calls = [
            call for call in mock_http.post.call_args_list if call[0][0] == "/segments"
        ]
        self.assertEqual(len(submission_calls), 3)
        self.assertEqual(len(submission_calls[0][1]["json"]["results"]), 10)
        self.assertEqual(len(submission_calls[1][1]["json"]["results"]), 10)
        self.assertEqual(len(submission_calls[2][1]["json"]["results"]), 5)

    def test_job_processor_chunk_error_isolation_and_recovery(self):
        mock_http = MagicMock()
        pending_segments = [
            {"id": "seg-1", "segment_index": 0, "source_text": "Failing sentence"},
            {"id": "seg-2", "segment_index": 1, "source_text": "Working sentence"},
        ]
        mock_http.get.side_effect = [
            fake_resp(200, json={"segments": pending_segments}),
            fake_resp(200, json={"segments": []}),
        ]
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        mock_pipeline = MagicMock()

        call_count = 0

        def failing_run_batch(texts, sl="en", tl="bn"):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:  # Fails for first chunk (attempt 1 and retry 2)
                raise TimeoutError("Engine timed out")
            from app.pipeline import Result
            return [Result(text="অনূদিত বাক্য")], {}

        mock_pipeline.run_batch.side_effect = failing_run_batch

        # chunk_size=1 so each segment is processed in its own chunk
        processor = JobProcessor(client, mock_pipeline, batch_size=10, chunk_size=1, max_chunk_retries=1)
        job = {
            "id": "job-isolate-1",
            "user_id": "user-1",
            "file_name": "test.docx",
            "original_file_path": "",
            "total_segments": 2,
        }

        # The job must NOT crash despite chunk 1 failing
        processor.process(job)

        submission_calls = [
            call for call in mock_http.post.call_args_list if call[0][0] == "/segments"
        ]
        self.assertEqual(len(submission_calls), 2)
        # First chunk fell back safely to source text
        self.assertEqual(submission_calls[0][1]["json"]["results"][0]["target_text"], "Failing sentence")
        # Second chunk was translated
        self.assertEqual(submission_calls[1][1]["json"]["results"][0]["target_text"], "অনূদিত বাক্য")

        # Job completed status
        status_calls = [call for call in mock_http.post.call_args_list if call[0][0] == "/status"]
        self.assertEqual(status_calls[-1][1]["json"]["status"], "completed")

    def test_job_processor_applies_glossary_protection(self):
        mock_http = MagicMock()
        pending_segments = [
            {"id": "seg-1", "segment_index": 0, "source_text": "The Safety Coordinator approved the Safety Guidelines."}
        ]
        mock_http.get.side_effect = [
            fake_resp(200, json={"segments": pending_segments}),
            fake_resp(200, json={"segments": []}),
        ]
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        mock_pipeline = MagicMock()

        received_batch_texts = []

        def fake_run_batch(texts, sl="en", tl="bn"):
            received_batch_texts.extend(texts)
            from app.pipeline import Result
            # Simulates model preserving {0} and {1} in translation
            return [Result(text="{0} {1} অনুমোদন করেছেন।")], {}

        mock_pipeline.run_batch.side_effect = fake_run_batch

        processor = JobProcessor(client, mock_pipeline, batch_size=10, chunk_size=5)
        job = {
            "id": "job-glossary-1",
            "user_id": "user-1",
            "file_name": "test.docx",
            "original_file_path": "",
            "total_segments": 1,
            "glossary": {
                "Safety Coordinator": "নিরাপত্তা সমন্বয়কারী",
                "Safety Guidelines": "নিরাপত্তা নির্দেশিকা",
            },
        }

        processor.process(job)

        # Verify pipeline received protected placeholder tokens instead of raw terms
        self.assertEqual(len(received_batch_texts), 1)
        self.assertIn("{0}", received_batch_texts[0])
        self.assertIn("{1}", received_batch_texts[0])
        self.assertNotIn("Safety Coordinator", received_batch_texts[0])

        # Verify submission has terms properly restored into target_text
        submission_calls = [
            call for call in mock_http.post.call_args_list if call[0][0] == "/segments"
        ]
        self.assertEqual(len(submission_calls), 1)
        saved_target = submission_calls[0][1]["json"]["results"][0]["target_text"]
        self.assertIn("নিরাপত্তা সমন্বয়কারী", saved_target)
        self.assertIn("নিরাপত্তা নির্দেশিকা", saved_target)

    def test_job_processor_uses_tm_exact_match(self):
        """Test that exact TM matches bypass pipeline and glossary protection."""
        mock_http = MagicMock()
        pending_segments = [
            {"id": "seg-1", "segment_index": 0, "source_text": "Hello world"},
            {"id": "seg-2", "segment_index": 1, "source_text": "New segment without TM"},
            {"id": "seg-3", "segment_index": 2, "source_text": "Second paragraph"},
        ]
        mock_http.get.side_effect = [
            fake_resp(200, json={"segments": pending_segments}),
            fake_resp(200, json={"segments": []}),
        ]
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        mock_pipeline = MagicMock()

        received_batch_texts = []

        def fake_run_batch(texts, sl="en", tl="bn"):
            received_batch_texts.extend(texts)
            from app.pipeline import Result
            return [Result(text="অনূদিত বাক্য")], {}

        mock_pipeline.run_batch.side_effect = fake_run_batch

        processor = JobProcessor(client, mock_pipeline, batch_size=10, chunk_size=5)
        job = {
            "id": "job-tm-1",
            "user_id": "user-1",
            "file_name": "test.docx",
            "original_file_path": "",
            "total_segments": 3,
            "source_language": "en",
            "target_language": "bn",
            "tm": {
                "Hello world": "হ্যালো বিশ্ব",
                "Second paragraph": "দ্বিতীয় অনুচ্ছেদ",
            },
        }

        processor.process(job)

        # Verify pipeline only received the segment without TM match
        self.assertEqual(len(received_batch_texts), 1)
        self.assertEqual(received_batch_texts[0], "New segment without TM")

        # Verify submission has correct statuses and translations
        submission_calls = [
            call for call in mock_http.post.call_args_list if call[0][0] == "/segments"
        ]
        self.assertEqual(len(submission_calls), 1)
        results = submission_calls[0][1]["json"]["results"]

        # Check TM segments have status "tm" and correct translations
        tm_results = [r for r in results if r["status"] == "tm"]
        self.assertEqual(len(tm_results), 2)
        self.assertEqual(tm_results[0]["target_text"], "হ্যালো বিশ্ব")
        self.assertEqual(tm_results[1]["target_text"], "দ্বিতীয় অনুচ্ছেদ")

        # Check pipeline segment has status "translated"
        mt_results = [r for r in results if r["status"] == "translated"]
        self.assertEqual(len(mt_results), 1)
        self.assertEqual(mt_results[0]["target_text"], "অনূদিত বাক্য")

    def test_normalize_text_function(self):
        """Test normalize_text matches browser's norm() behavior."""
        from app.textutils import normalize_text

        # Whitespace collapse and trim
        self.assertEqual(normalize_text("  Hello   world  "), "Hello world")

        # Zero-width space removal
        self.assertEqual(normalize_text("Hello\u200Bworld"), "Helloworld")

        # BOM removal
        self.assertEqual(normalize_text("\uFEFFHello world"), "Hello world")

        # Combined normalization
        self.assertEqual(normalize_text("  Hello\u200B   world  "), "Hello world")

        # Empty string
        self.assertEqual(normalize_text(""), "")

        # Already normalized
        self.assertEqual(normalize_text("Hello world"), "Hello world")

    def test_update_tm_via_supabase(self):
        """Test TM update via direct Supabase fallback."""
        from unittest.mock import patch

        client = LovableWorkerClient(self.config)
        updates = {"new key": "new target", "existing": "updated target"}

        with patch("app.worker.httpx") as mock_httpx:
            # Simulate existing TM fetch
            mock_httpx.get.return_value = fake_resp(200, json=[{"tm": {"existing": "existing target"}}])
            # Simulate successful update
            mock_httpx.patch.return_value = fake_resp(200, json={})

            result = client._update_tm_via_supabase("project-123", updates)

            self.assertTrue(result)
            # Verify PATCH was called with merged TM
            mock_httpx.patch.assert_called_once()
            call_args = mock_httpx.patch.call_args
            merged_tm = call_args[1]["json"]["tm"]
            self.assertEqual(merged_tm["existing"], "updated target")
            self.assertEqual(merged_tm["new key"], "new target")

    def test_update_tm_empty_updates(self):
        """Test TM update with empty updates returns False."""
        client = LovableWorkerClient(self.config)
        result = client.update_tm("project-123", {})
        self.assertFalse(result)

    def test_update_tm_no_project_id(self):
        """Test TM update without project_id returns False."""
        client = LovableWorkerClient(self.config)
        result = client.update_tm("", {"key": "value"})
        self.assertFalse(result)

    def test_job_processor_tracks_tm_and_pipeline_counts(self):
        """Test that job processor correctly tracks TM hits vs pipeline translations."""
        mock_http = MagicMock()
        pending_segments = [
            {"id": "seg-1", "segment_index": 0, "source_text": "Hello world"},
            {"id": "seg-2", "segment_index": 1, "source_text": "New segment"},
            {"id": "seg-3", "segment_index": 2, "source_text": "Another segment"},
        ]
        mock_http.get.side_effect = [
            fake_resp(200, json={"segments": pending_segments}),
            fake_resp(200, json={"segments": []}),
        ]
        mock_http.post.return_value = fake_resp(200, json={"ok": True})

        client = LovableWorkerClient(self.config, client=mock_http)
        mock_pipeline = MagicMock()

        received_batch_texts = []

        def fake_run_batch(texts, sl="en", tl="bn"):
            received_batch_texts.extend(texts)
            from app.pipeline import Result
            return [Result(text="অনূদিত বাক্য")], {}

        mock_pipeline.run_batch.side_effect = fake_run_batch

        processor = JobProcessor(client, mock_pipeline, batch_size=10, chunk_size=5)
        job = {
            "id": "job-counts-1",
            "user_id": "user-1",
            "file_name": "test.docx",
            "original_file_path": "",
            "total_segments": 3,
            "source_language": "en",
            "target_language": "bn",
            "tm": {
                "Hello world": "হ্যালো বিশ্ব",  # This will be a TM hit
            },
        }

        processor.process(job)

        # Verify TM hit was NOT sent to pipeline
        self.assertNotIn("Hello world", received_batch_texts)
        # Verify the other segments were sent to pipeline
        self.assertIn("New segment", received_batch_texts)
        self.assertIn("Another segment", received_batch_texts)

    def test_parse_tm_payload_direct_map(self):
        """Test TM parsing with direct source->target map."""
        raw = {
            "Hello world": "হ্যালো বিশ্ব",
            "Second paragraph": "দ্বিতীয় অনুচ্ছেদ",
        }
        result = LovableWorkerClient._parse_tm_payload(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["Hello world"], "হ্যালো বিশ্ব")
        self.assertEqual(result["Second paragraph"], "দ্বিতীয় অনুচ্ছেদ")

    def test_parse_tm_payload_list_format(self):
        """Test TM parsing with list of dicts format."""
        raw = [
            {"src": "Hello world", "tgt": "হ্যালো বিশ্ব"},
            {"source": "Second paragraph", "target": "দ্বিতীয় অনুচ্ছেদ"},
        ]
        result = LovableWorkerClient._parse_tm_payload(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["Hello world"], "হ্যালো বিশ্ব")
        self.assertEqual(result["Second paragraph"], "দ্বিতীয় অনুচ্ছেদ")

    def test_parse_tm_payload_normalization(self):
        """Test TM key normalization (NFC, ZWSP removal, whitespace collapse)."""
        raw = {
            "Hello   world": "হ্যালো বিশ্ব",  # Multiple spaces
            "  Hello  world  ": "হ্যালো বিশ্ব",  # Leading/trailing spaces
        }
        result = LovableWorkerClient._parse_tm_payload(raw)
        # Both should normalize to the same key (last wins per browser behavior)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["Hello world"], "হ্যালো বিশ্ব")

    def test_parse_tm_payload_zwsp_removal(self):
        """Test TM key normalization removes zero-width spaces."""
        raw = {
            "Hello\u200Bworld": "হ্যালো বিশ্ব",  # Zero-width space in middle
        }
        result = LovableWorkerClient._parse_tm_payload(raw)
        # ZWSP should be removed, resulting in "Helloworld"
        self.assertEqual(len(result), 1)
        self.assertIn("Helloworld", result)
        self.assertEqual(result["Helloworld"], "হ্যালো বিশ্ব")

    def test_parse_tm_payload_nested_structure(self):
        """Test TM parsing when nested in cat_projects structure."""
        raw = {"tm": {"Hello world": "হ্যালো বিশ্ব"}}
        result = LovableWorkerClient._parse_tm_payload(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["Hello world"], "হ্যালো বিশ্ব")

    def test_parse_tm_payload_empty_and_invalid(self):
        """Test TM parsing with empty/invalid entries."""
        raw = {
            "": "target",  # Empty key
            "source": "",  # Empty value
            "   ": "target",  # Whitespace-only key
            "source": "   ",  # Whitespace-only value
            "valid": "valid target",
        }
        result = LovableWorkerClient._parse_tm_payload(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["valid"], "valid target")


if __name__ == "__main__":
    unittest.main()

