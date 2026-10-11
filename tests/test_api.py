"""HTTP-level tests. They need `pip install -r requirements-dev.txt` and are skipped without it."""
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE_WEB = True
except Exception:  # fastapi or httpx not installed
    HAVE_WEB = False

from app.engines.base import EngineError

from .helpers import TOKEN, FakeEngine, make_pipeline, make_settings

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx not installed")
class Api(unittest.TestCase):
    def client(self, engines=None, **settings):
        from app.main import create_app
        engines = engines or [FakeEngine("one"), FakeEngine("two")]
        pipe, clock, _ = make_pipeline(engines)
        names = [e.name for e in engines]
        s = make_settings(names, allowed_origins=("https://app.example.com",), **settings)
        pipe.s = s
        return TestClient(create_app(s, pipe)), pipe, clock

    def test_health_is_open_and_everything_else_needs_the_token(self):
        c, _, _ = self.client()
        self.assertEqual(c.get("/api/health").json()["ok"], True)
        for method, path in (("get", "/api/engines"), ("get", "/api/selftest"), ("post", "/api/translate")):
            r = getattr(c, method)(path)
            self.assertEqual(r.status_code, 401, path)
        self.assertEqual(c.get("/api/engines", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(c.get("/api/engines", headers=AUTH).status_code, 200)

    def test_translate_returns_a_result_per_segment(self):
        c, _, _ = self.client()
        r = c.post("/api/translate", headers=AUTH, json={"sl": "en", "tl": "bn", "segments": ["Hello there", "12"]})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["results"][0]["text"], "one:Hello there")
        self.assertEqual(body["results"][0]["via"], "one")
        self.assertEqual(body["results"][1]["via"], "passthrough")
        self.assertIn("elapsed_ms", body["meta"])

    def test_one_failed_segment_does_not_fail_the_request(self):
        one = FakeEngine("one", [EngineError("error", "kaput")] * 2)
        c, _, _ = self.client([one])
        r = c.post("/api/translate", headers=AUTH, json={"segments": ["Hello there", "Second one"]})
        results = r.json()["results"]
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(results[0]["text"])
        self.assertIn("kaput", results[0]["error"])
        self.assertEqual(results[1]["text"], "one:Second one")

    def test_bad_requests(self):
        c, _, _ = self.client()
        post = lambda body: c.post("/api/translate", headers=AUTH, json=body).status_code
        self.assertEqual(post({"segments": []}), 400)
        self.assertEqual(post({"sl": "e;n", "segments": ["x"]}), 400)
        self.assertEqual(post({"segments": ["x"] * 11}), 413)
        self.assertEqual(post({"segments": ["Hello there"], "engines": ["nope"]}), 400)
        self.assertEqual(post({}), 422)

    def test_selftest_and_its_rate_limit(self):
        c, _, _ = self.client()
        first = c.get("/api/selftest", headers=AUTH)
        self.assertEqual(first.status_code, 200)
        self.assertEqual({r["engine"] for r in first.json()["results"]}, {"one", "two"})
        again = c.get("/api/selftest", headers=AUTH)
        self.assertEqual(again.status_code, 429)
        self.assertIn("retry-after", again.headers)

    def test_cors(self):
        c, _, _ = self.client()
        ask = lambda origin: c.options("/api/translate", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(ask("https://app.example.com").headers.get("access-control-allow-origin"), "https://app.example.com")
        self.assertEqual(ask("http://localhost:5173").headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertNotIn("access-control-allow-origin", ask("https://evil.example").headers)

    def test_docs_are_off_by_default(self):
        c, _, _ = self.client()
        self.assertEqual(c.get("/docs").status_code, 404)
        self.assertEqual(c.get("/openapi.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
