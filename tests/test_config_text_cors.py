import re
import unittest

from app.config import ConfigError, Settings, parse_delays, parse_steps
from app.cors import build_cors
from app.textutils import chunk_text, has_letters, scrub
from app.validation import RequestError, validate_langs, validate_translate

from .helpers import make_settings

TOKEN = "a-long-random-secret-1234"


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_env({"API_TOKEN": TOKEN})
        self.assertEqual(s.engine_order, ("gemini", "cloudflare", "mymemory"))
        self.assertEqual(s.max_segments, 10)
        self.assertTrue(s.allow_localhost)
        self.assertFalse(s.enable_docs)
        self.assertEqual(s.cooldown_steps, (300, 900, 3600))

    def test_token_is_required_and_must_be_long(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({})
        with self.assertRaises(ConfigError):
            Settings.from_env({"API_TOKEN": "short"})

    def test_overrides_are_read(self):
        s = Settings.from_env({
            "API_TOKEN": TOKEN, "ENGINE_ORDER": "mymemory, google_web", "ALLOWED_ORIGINS": "https://a.com, https://b.com",
            "ALLOW_LOCALHOST": "false", "BREAKER_THRESHOLD": "5", "ENGINE_DELAYS": "mymemory:1-2",
            "COOLDOWN_STEPS": "60,120", "ENABLE_DOCS": "yes",
        })
        self.assertEqual(s.engine_order, ("mymemory", "google_web"))
        self.assertEqual(s.allowed_origins, ("https://a.com", "https://b.com"))
        self.assertFalse(s.allow_localhost)
        self.assertEqual(s.breaker_threshold, 5)
        self.assertEqual(s.delays["mymemory"], (1.0, 2.0))
        self.assertEqual(s.delays["google_web"], (0.6, 1.4))  # others keep their defaults
        self.assertEqual(s.cooldown_steps, (60, 120))
        self.assertTrue(s.enable_docs)

    def test_bad_values_explain_themselves(self):
        for env in ({"ENGINE_RETRIES": "many"}, {"ENGINE_RETRIES": "99"}, {"ALLOW_LOCALHOST": "maybe"},
                    {"LOG_LEVEL": "LOUD"}, {"ENGINE_DELAYS": "google_web:2-1"}, {"COOLDOWN_STEPS": "a,b"}):
            with self.assertRaises(ConfigError, msg=str(env)):
                Settings.from_env({"API_TOKEN": TOKEN, **env})

    def test_parsers(self):
        self.assertEqual(parse_delays("x:0.5-1")["x"], (0.5, 1.0))
        self.assertEqual(parse_steps("1,2,3"), (1, 2, 3))
        self.assertEqual(parse_steps(None), (300, 900, 3600))


class TextTests(unittest.TestCase):
    def test_has_letters(self):
        self.assertTrue(has_letters("Hello"))
        self.assertTrue(has_letters("\u09a8\u09ae\u09b8\u09cd\u0995\u09be\u09b0"))
        self.assertFalse(has_letters("12,345.67 - (8)"))
        self.assertFalse(has_letters("  "))

    def test_scrub_removes_text_queries_and_shortens(self):
        msg = scrub("bad: Hello world here at /x?sl=en&q=Hello%20world%20here", "Hello world here")
        self.assertNotIn("Hello", msg)
        self.assertNotIn("world", scrub("see Hello%20world%20here in the log", "Hello world here"))
        self.assertNotIn("world", scrub("got q=Hello%20world%20here", "unrelated text"))
        self.assertEqual(scrub("x" * 500, "")[-1], "\u2026")
        self.assertLessEqual(len(scrub("x" * 500, "")), 200)
        self.assertEqual(scrub("ok", "hi"), "ok")  # very short text is left alone

    def test_chunking_keeps_text_and_respects_the_limit(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(60))
        chunks = chunk_text(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(" ".join(chunks), text)
        self.assertEqual(chunk_text("short", 100), ["short"])

    def test_chunking_handles_bengali_danda_and_giant_words(self):
        bn = " ".join("\u0986\u09ae\u09bf \u09ad\u09be\u09a4 \u0996\u09be\u0987\u0964" for _ in range(40))
        parts = chunk_text(bn, 60)
        self.assertTrue(all(len(c) <= 60 for c in parts))
        self.assertEqual(" ".join(parts), bn)
        giant = chunk_text("x" * 250, 100)
        self.assertEqual(["".join(giant)], ["x" * 250])
        self.assertTrue(all(len(c) <= 100 for c in giant))


class CorsTests(unittest.TestCase):
    def test_exact_and_wildcard_origins(self):
        exact, regex = build_cors(("https://app.example.com/", "https://*.lovable.app"), allow_localhost=False)
        self.assertEqual(exact, ["https://app.example.com"])
        for ok in ("https://abc.lovable.app", "https://id-preview--x1.lovable.app", "https://a.b.lovable.app"):
            self.assertTrue(re.fullmatch(regex, ok), ok)
        for bad in ("https://lovable.app", "https://evil.com", "https://lovable.app.evil.com", "http://abc.lovable.app"):
            self.assertFalse(re.fullmatch(regex, bad), bad)

    def test_localhost_switch(self):
        _, regex = build_cors((), allow_localhost=True)
        self.assertTrue(re.fullmatch(regex, "http://localhost:5173"))
        self.assertTrue(re.fullmatch(regex, "http://127.0.0.1:8080"))
        self.assertFalse(re.fullmatch(regex, "https://localhost.evil.com"))
        self.assertEqual(build_cors((), allow_localhost=False), ([], None))

    def test_bad_entries_are_rejected(self):
        for bad in ("*", "example.com"):
            with self.assertRaises(ConfigError):
                build_cors((bad,), True)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.s = make_settings(["one"], max_segments=3, max_segment_chars=20, max_request_chars=40)

    def status(self, sl, tl, segments):
        with self.assertRaises(RequestError) as ctx:
            validate_translate(self.s, sl, tl, segments)
        return ctx.exception.status

    def test_rules(self):
        validate_translate(self.s, "en", "bn", ["fine", "also fine"])
        self.assertEqual(self.status("en", "bn", []), 400)
        self.assertEqual(self.status("e;n", "bn", ["x"]), 400)
        self.assertEqual(self.status("en", "en", ["x"]), 400)
        self.assertEqual(self.status("en", "bn", ["a"] * 4), 413)
        self.assertEqual(self.status("en", "bn", ["x" * 21]), 413)
        self.assertEqual(self.status("en", "bn", ["x" * 15] * 3), 413)
        validate_langs("en", "zh-CN")


if __name__ == "__main__":
    unittest.main()
