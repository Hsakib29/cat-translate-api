import sys
import types
import unittest
from unittest import mock

from app.engines.base import EngineError
from app.engines.errors import classify_exception
from app.engines.google_gtx import GoogleGtxEngine, parse_gtx_response
from app.engines.google_web import GoogleWebEngine
from app.engines.mymemory import MyMemoryEngine, check_mymemory_text, locale_for


def named(name, base=Exception):
    return type(name, (base,), {})


def fake_deep_translator(google=None, mymemory=None):
    module = types.ModuleType("deep_translator")
    module.GoogleTranslator = google
    module.MyMemoryTranslator = mymemory
    return module


class Classification(unittest.TestCase):
    def kind(self, exc):
        return classify_exception(exc).kind

    def test_library_exceptions_map_by_name(self):
        self.assertEqual(self.kind(named("TooManyRequests")("429")), "rate_limited")
        self.assertEqual(self.kind(named("TranslationNotFound")("none")), "empty")
        self.assertEqual(self.kind(named("ElementNotFoundInGetRequest")("none")), "empty")
        self.assertEqual(self.kind(named("LanguageNotSupportedException")("xx")), "unsupported")
        self.assertEqual(self.kind(named("NotValidLength")("long")), "bad_request")
        self.assertEqual(self.kind(ValueError("odd")), "error")

    def test_network_problems(self):
        import requests
        self.assertEqual(self.kind(requests.exceptions.ConnectionError("down")), "network")
        self.assertEqual(self.kind(requests.exceptions.ReadTimeout("slow")), "network")
        self.assertEqual(self.kind(TimeoutError("slow")), "network")

    def test_engine_errors_pass_through(self):
        err = EngineError("blocked", "page")
        self.assertIs(classify_exception(err), err)


class GtxParsing(unittest.TestCase):
    def kind(self, status, ctype, body):
        with self.assertRaises(EngineError) as ctx:
            parse_gtx_response(status, ctype, body)
        return ctx.exception.kind

    def test_good_answer_joins_the_parts(self):
        body = '[[["\u09a8\u09ae\u09b8\u09cd\u0995\u09be\u09b0 ","Hello ",null],["\u09ac\u09bf\u09b6\u09cd\u09ac","world",null]],null,"en"]'
        self.assertEqual(parse_gtx_response(200, "application/json", body), "\u09a8\u09ae\u09b8\u09cd\u0995\u09be\u09b0 \u09ac\u09bf\u09b6\u09cd\u09ac")

    def test_failures_are_told_apart(self):
        sorry = "<html><body>We're sorry... automated queries</body></html>"
        self.assertEqual(self.kind(200, "text/html; charset=utf-8", sorry), "blocked")
        self.assertEqual(self.kind(429, "text/html", sorry), "rate_limited")
        self.assertEqual(self.kind(403, "application/json", "{}"), "blocked")
        self.assertEqual(self.kind(503, "application/json", "{}"), "error")
        self.assertEqual(self.kind(200, "application/json", "not json"), "error")
        self.assertEqual(self.kind(200, "application/json", "[[[null,null]]]"), "empty")


class GtxRequests(unittest.TestCase):
    def response(self):
        return types.SimpleNamespace(status_code=200, headers={"content-type": "application/json"}, text='[[["\u09b9\u09be\u0987","hi",null]]]')

    def test_short_text_uses_get_and_long_text_uses_post(self):
        engine = GoogleGtxEngine()
        with mock.patch("requests.get", return_value=self.response()) as get, mock.patch("requests.post", return_value=self.response()) as post:
            engine.translate("Hello there", "en", "bn", 5)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.kwargs["params"]["q"], "Hello there")
            engine.translate("\u0995" * 3000, "bn", "en", 5)  # each Bengali letter is 9 characters once encoded
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["data"]["q"], "\u0995" * 3000)
            self.assertNotIn("q", post.call_args.kwargs["params"])

    def test_network_failure_becomes_a_network_error(self):
        import requests
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectionError("down")):
            with self.assertRaises(EngineError) as ctx:
                GoogleGtxEngine().translate("Hello there", "en", "bn", 5)
        self.assertEqual(ctx.exception.kind, "network")


class GoogleWeb(unittest.TestCase):
    def run_engine(self, translator_cls):
        with mock.patch.dict(sys.modules, {"deep_translator": fake_deep_translator(google=translator_cls)}):
            return GoogleWebEngine().translate("Hello there", "en", "bn", 5)

    def test_translates(self):
        class T:
            def __init__(self, source, target):
                self.pair = (source, target)

            def translate(self, text):
                return f"{self.pair[1]}:{text}"

        self.assertEqual(self.run_engine(T), "bn:Hello there")

    def test_empty_and_exceptions_are_classified(self):
        class Empty:
            def __init__(self, source, target): pass
            def translate(self, text): return "  "

        class Limited:
            def __init__(self, source, target): pass
            def translate(self, text): raise named("TooManyRequests")("429")

        for cls, kind in ((Empty, "empty"), (Limited, "rate_limited")):
            with self.assertRaises(EngineError) as ctx:
                self.run_engine(cls)
            self.assertEqual(ctx.exception.kind, kind)


class MyMemory(unittest.TestCase):
    def test_locales(self):
        self.assertEqual(locale_for("bn"), "bn-IN")
        self.assertEqual(locale_for("en-GB"), "en-GB")
        self.assertEqual(locale_for("EN"), "en-GB")
        self.assertIsNone(locale_for("xx"))
        self.assertTrue(MyMemoryEngine().supports("en", "bn"))
        self.assertFalse(MyMemoryEngine().supports("en", "xx"))

    def test_problems_reported_inside_the_text(self):
        for text, kind in (("MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY", "rate_limited"),
                           ("PLEASE SELECT TWO DISTINCT LANGUAGES", "unsupported"), ("", "empty"), (None, "empty")):
            with self.assertRaises(EngineError) as ctx:
                check_mymemory_text(text)
            self.assertEqual(ctx.exception.kind, kind)
        self.assertEqual(check_mymemory_text("fine"), "fine")

    def test_long_text_is_sent_in_small_pieces_and_joined(self):
        sent = []

        class T:
            def __init__(self, source, target):
                sent.append((source, target))

            def translate(self, text):
                sent.append(text)
                return "T(" + text[:10] + ")"

        long_text = " ".join(f"This is sentence number {i}." for i in range(60))
        with mock.patch.dict(sys.modules, {"deep_translator": fake_deep_translator(mymemory=T)}):
            out = MyMemoryEngine().translate(long_text, "en", "bn", 5)
        self.assertEqual(sent[0], ("en-GB", "bn-IN"))
        pieces = sent[1:]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 450 for p in pieces))
        self.assertEqual(out.count("T("), len(pieces))

    def test_unmapped_language_is_unsupported(self):
        with self.assertRaises(EngineError) as ctx:
            MyMemoryEngine().translate("Hello there", "en", "xx", 5)
        self.assertEqual(ctx.exception.kind, "unsupported")


if __name__ == "__main__":
    unittest.main()
