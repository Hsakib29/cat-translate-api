"""Unit tests for Gemini and Cloudflare translation engines with mocked responses."""
import json
import types
import unittest
from unittest import mock

from app.engines.base import EngineError
from app.engines.cloudflare import CloudflareEngine
from app.engines.gemini import GeminiEngine
from app.engines.mymemory import MyMemoryEngine


def mock_response(status_code: int = 200, json_data: dict | None = None, text: str = ""):
    resp = types.SimpleNamespace()
    resp.status_code = status_code
    resp.text = text or (json.dumps(json_data) if json_data is not None else "")
    resp.json = lambda: json_data if json_data is not None else json.loads(resp.text)
    return resp


class TestGeminiEngine(unittest.TestCase):
    def test_missing_api_key_raises_unsupported(self):
        engine = GeminiEngine(api_key="")
        self.assertFalse(engine.supports("en", "bn"))
        with self.assertRaises(EngineError) as ctx:
            engine.translate("Hello", "en", "bn", 5)
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_successful_translation_with_thought_filtration(self):
        # Candidate containing thought block and Bengali digits
        candidate_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "Reasoning about translation..."},
                            {"thought": False, "text": json.dumps({"1": "নথি ১০২ খুলুন {0}।"})},
                        ]
                    }
                }
            ]
        }
        engine = GeminiEngine(api_key="fake-test-key", models=("gemini-3.5-flash-lite",))
        with mock.patch("requests.post", return_value=mock_response(200, candidate_data)):
            result = engine.translate("Open document 102 {0}.", "en", "bn", 5)
            # Digits normalized to ASCII 0-9
            self.assertIn("102", result)
            # Placeholder {0} preserved
            self.assertIn("{0}", result)
            self.assertTrue(result.startswith("নথি 102"))

    def test_inline_xml_thought_blocks_are_stripped(self):
        candidate_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "thought": False,
                                "text": "<thought>Thinking here...</thought>\n" + json.dumps({"1": "স্বাগতম {0}"}),
                            }
                        ]
                    }
                }
            ]
        }
        engine = GeminiEngine(api_key="fake-test-key", models=("gemini-3.5-flash-lite",))
        with mock.patch("requests.post", return_value=mock_response(200, candidate_data)):
            result = engine.translate("Welcome {0}", "en", "bn", 5)
            self.assertEqual(result, "স্বাগতম {0}")

    def test_lost_tag_raises_error(self):
        # Output is missing {0}
        candidate_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": False, "text": json.dumps({"1": "স্বাগতম"})},
                        ]
                    }
                }
            ]
        }
        engine = GeminiEngine(api_key="fake-test-key", models=("gemini-3.5-flash-lite",))
        with mock.patch("requests.post", return_value=mock_response(200, candidate_data)):
            with self.assertRaises(EngineError) as ctx:
                engine.translate("Welcome {0}", "en", "bn", 5)
            self.assertEqual(ctx.exception.kind, "empty")
            self.assertIn("lost tag '{0}'", ctx.exception.message)

    def test_model_switching_cascade_on_rate_limit(self):
        # Model 1 gives 429, Model 2 succeeds
        resp_429 = mock_response(429, text="quota exceeded")
        resp_200 = mock_response(200, {
            "candidates": [
                {"content": {"parts": [{"thought": False, "text": json.dumps({"1": "মডেল ২ উত্তর"})}]}}
            ]
        })
        sleep_calls = []
        engine = GeminiEngine(
            api_key="fake-test-key",
            models=("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
            delays=(4.0, 5.0),
            sleep=lambda s: sleep_calls.append(s),
        )
        with mock.patch("requests.post", side_effect=[resp_429, resp_200]) as mock_post:
            result = engine.translate("Hello", "en", "bn", 5)
            self.assertEqual(result, "মডেল 2 উত্তর")
            self.assertEqual(mock_post.call_count, 2)
            self.assertEqual(sleep_calls, [4.0])  # intra-engine pacing pause


class TestCloudflareEngine(unittest.TestCase):
    def test_missing_credentials_raises_unsupported(self):
        engine = CloudflareEngine(account_id="", api_token="")
        self.assertFalse(engine.supports("en", "bn"))
        with self.assertRaises(EngineError) as ctx:
            engine.translate("Hello", "en", "bn", 5)
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_successful_translation(self):
        engine = CloudflareEngine(account_id="fake-acc", api_token="fake-tok")
        self.assertTrue(engine.supports("en", "bn"))
        resp = mock_response(200, {"result": {"translated_text": "অনুবাদ {0}"}})
        with mock.patch("requests.post", return_value=resp):
            result = engine.translate("Hello {0}", "en", "bn", 5)
            self.assertEqual(result, "অনুবাদ {0}")

    def test_lost_tag_raises_empty(self):
        engine = CloudflareEngine(account_id="fake-acc", api_token="fake-tok")
        resp = mock_response(200, {"result": {"translated_text": "অনুবাদ"}})  # missing {0}
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(EngineError) as ctx:
                engine.translate("Hello {0}", "en", "bn", 5)
            self.assertEqual(ctx.exception.kind, "empty")
            self.assertIn("lost tag '{0}'", ctx.exception.message)


class TestMyMemoryWithEmail(unittest.TestCase):
    def test_email_is_passed_to_translator(self):
        engine = MyMemoryEngine(email="test@example.com")
        self.assertEqual(engine.email, "test@example.com")


if __name__ == "__main__":
    unittest.main()
