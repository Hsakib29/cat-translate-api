import json
import threading
import time
import unittest
from unittest import mock

from app import pipeline as pipeline_module
from app.engines.base import Engine, EngineError
from app.pipeline import Busy, TooSoon, UnknownEngine

from .helpers import FakeEngine, make_pipeline, make_real_time_pipeline

EN, BN = "en", "bn"


class ChainAndCache(unittest.TestCase):
    def test_first_engine_answers_and_result_is_cached(self):
        one, two = FakeEngine("one"), FakeEngine("two")
        pipe, _, _ = make_pipeline([one, two])
        first, _ = pipe.run_batch(["Hello there"], EN, BN)
        again, _ = pipe.run_batch(["Hello there"], EN, BN)
        self.assertEqual(first[0].text, "one:Hello there")
        self.assertEqual(first[0].via, "one")
        self.assertTrue(again[0].cached)
        self.assertEqual(len(one.calls), 1)

    def test_failures_are_never_cached(self):
        one = FakeEngine("one", [EngineError("empty"), EngineError("empty")])
        pipe, _, _ = make_pipeline([one])
        r1, _ = pipe.run_batch(["Hello there"], EN, BN)
        r2, _ = pipe.run_batch(["Hello there"], EN, BN)
        self.assertIsNone(r1[0].text)
        self.assertIsNone(r2[0].text)
        self.assertEqual(len(one.calls), 2)

    def test_cache_can_be_turned_off_and_evicts_oldest(self):
        one = FakeEngine("one")
        pipe, _, _ = make_pipeline([one], cache_size=0)
        pipe.run_batch(["Hello there"], EN, BN)
        pipe.run_batch(["Hello there"], EN, BN)
        self.assertEqual(len(one.calls), 2)
        small = FakeEngine("one")
        pipe2, _, _ = make_pipeline([small], cache_size=2)
        pipe2.run_batch(["alpha one", "beta two", "gamma three"], EN, BN)
        pipe2.run_batch(["alpha one"], EN, BN)  # was evicted, so it is translated again
        self.assertEqual(small.calls.count("alpha one"), 2)

    def test_network_error_is_retried_then_falls_back(self):
        one = FakeEngine("one", [EngineError("network", "boom"), EngineError("network", "boom")])
        two = FakeEngine("two")
        pipe, clock, _ = make_pipeline([one, two])
        results, _ = pipe.run_batch(["Hello there"], EN, BN)
        self.assertEqual(results[0].via, "two")
        self.assertEqual(len(one.calls), 2)          # first try plus one retry
        self.assertEqual(clock.sleeps, [2.0])        # one backoff pause
        self.assertEqual(results[0].attempts[0]["engine"], "one")
        self.assertEqual(results[0].attempts[0]["kind"], "network")

    def test_request_can_choose_the_chain(self):
        one, two = FakeEngine("one"), FakeEngine("two")
        pipe, _, _ = make_pipeline([one, two])
        results, meta = pipe.run_batch(["Hello there"], EN, BN, chain=["two", "one", "two"])
        self.assertEqual(results[0].via, "two")
        self.assertEqual(meta["engines"], ["two", "one"])
        with self.assertRaises(UnknownEngine):
            pipe.run_batch(["Hello there"], EN, BN, chain=["nope"])

    def test_unknown_engine_in_default_order_fails_fast(self):
        from app.pipeline import Pipeline
        from .helpers import make_settings
        with self.assertRaises(UnknownEngine):
            Pipeline({"one": FakeEngine("one")}, make_settings(["one", "ghost"]))

    def test_all_engines_failing_gives_a_readable_summary(self):
        one = FakeEngine("one", [EngineError("network", "no route")] * 2)
        two = FakeEngine("two", [EngineError("error", "kaput")] * 2)
        pipe, _, _ = make_pipeline([one, two])
        results, _ = pipe.run_batch(["Hello there"], EN, BN)
        self.assertIsNone(results[0].text)
        self.assertIn("one: no route", results[0].error)
        self.assertIn("two: kaput", results[0].error)
        self.assertTrue(results[0].retryable)

    def test_unsupported_pair_is_skipped_and_not_retryable(self):
        one = FakeEngine("one", languages={("en", "bn")})
        pipe, _, _ = make_pipeline([one])
        results, _ = pipe.run_batch(["Hello there"], "en", "hi")
        self.assertIsNone(results[0].text)
        self.assertFalse(results[0].retryable)
        self.assertEqual(one.calls, [])

    def test_numbers_blank_and_punctuation_pass_through(self):
        one = FakeEngine("one")
        pipe, _, _ = make_pipeline([one])
        results, _ = pipe.run_batch(["12,345", "   ", "\u2014 / \u2014"], EN, BN)
        self.assertEqual([r.via for r in results], ["passthrough"] * 3)
        self.assertEqual(results[0].text, "12,345")
        self.assertEqual(one.calls, [])

    def test_error_messages_never_contain_the_text(self):
        leak = EngineError("error", "failed for Hello secret text at /x?q=Hello%20secret%20text&sl=en")
        one = FakeEngine("one", [leak, leak])
        pipe, _, _ = make_pipeline([one])
        results, _ = pipe.run_batch(["Hello secret text"], EN, BN)
        self.assertNotIn("secret", results[0].error.lower())
        self.assertIn("<text>", results[0].error)

    def test_contextual_batch_translation_success_and_fallback(self):
        class BatchEngine(FakeEngine):
            def __init__(self, name, should_fail=False):
                super().__init__(name)
                self.should_fail = should_fail
                self.batch_calls = []

            def translate_batch(self, texts, sl, tl, timeout=None):
                self.batch_calls.append(texts)
                if self.should_fail:
                    raise RuntimeError("Batch LLM connection drop")
                return [f"batch:{t}" for t in texts]

        # Case 1: Batch translation succeeds
        batch_eng = BatchEngine("batch_eng")
        pipe, _, _ = make_pipeline([batch_eng])
        results, _ = pipe.run_batch(["First sentence", "Second sentence"], EN, BN)
        self.assertEqual(results[0].text, "batch:First sentence")
        self.assertEqual(results[1].text, "batch:Second sentence")
        self.assertEqual(len(batch_eng.batch_calls), 1)

        # Case 2: Batch translation fails, gracefully falls back to single-segment processing
        failing_batch = BatchEngine("failing_batch", should_fail=True)
        pipe2, _, _ = make_pipeline([failing_batch])
        results2, _ = pipe2.run_batch(["Seg A", "Seg B"], EN, BN)
        self.assertEqual(results2[0].text, "failing_batch:Seg A")
        self.assertEqual(results2[1].text, "failing_batch:Seg B")


class Cooldowns(unittest.TestCase):
    def test_block_page_trips_at_once_and_engine_is_skipped(self):
        one = FakeEngine("one", [EngineError("blocked", "robot page")])
        two = FakeEngine("two")
        pipe, clock, _ = make_pipeline([one, two])
        results, _ = pipe.run_batch(["Hello one", "Hello two"], EN, BN)
        self.assertEqual([r.via for r in results], ["two", "two"])
        self.assertEqual(one.calls, ["Hello one"])  # never hammered again
        self.assertEqual(results[1].attempts[0]["kind"], "cooldown")
        clock.advance(301)
        later, _ = pipe.run_batch(["Hello three"], EN, BN)
        self.assertEqual(later[0].via, "one")
        state = {e["name"]: e for e in pipe.snapshot()["engines"]}["one"]
        self.assertEqual(state["times_put_in_cooldown"], 0)  # a success resets the escalation

    def test_cooldown_grows_when_it_keeps_happening(self):
        one = FakeEngine("one", [EngineError("blocked")] * 2)
        two = FakeEngine("two")
        pipe, clock, _ = make_pipeline([one, two])
        pipe.run_batch(["Hello one"], EN, BN)
        clock.advance(301)
        pipe.run_batch(["Hello two"], EN, BN)
        left = {e["name"]: e for e in pipe.snapshot()["engines"]}["one"]["cooldown_remaining_seconds"]
        self.assertTrue(899 <= left <= 901, left)  # second step is 900 seconds

    def test_rate_limit_also_trips_at_once(self):
        one = FakeEngine("one", [EngineError("rate_limited", "429")])
        pipe, _, _ = make_pipeline([one, FakeEngine("two")])
        pipe.run_batch(["Hello one"], EN, BN)
        status = {e["name"]: e for e in pipe.snapshot()["engines"]}["one"]["status"]
        self.assertEqual(status, "cooldown")

    def test_empty_results_need_three_in_a_row(self):
        one = FakeEngine("one", [EngineError("empty"), EngineError("empty"), "fine", EngineError("empty"), EngineError("empty")])
        pipe, _, _ = make_pipeline([one, FakeEngine("two")])
        pipe.run_batch(["aa aa", "bb bb", "cc cc", "dd dd", "ee ee"], EN, BN)
        state = {e["name"]: e for e in pipe.snapshot()["engines"]}["one"]
        self.assertEqual(state["cooldown_remaining_seconds"], 0)
        self.assertEqual(state["consecutive_strikes"], 2)
        one2 = FakeEngine("one", [EngineError("empty")] * 3)
        pipe2, _, _ = make_pipeline([one2, FakeEngine("two")])
        pipe2.run_batch(["aa aa", "bb bb", "cc cc"], EN, BN)
        self.assertEqual({e["name"]: e for e in pipe2.snapshot()["engines"]}["one"]["status"], "cooldown")

    def test_bad_input_does_not_count_against_an_engine(self):
        one = FakeEngine("one", [EngineError("bad_request")] * 3)
        pipe, _, _ = make_pipeline([one])
        results, _ = pipe.run_batch(["aa aa", "bb bb", "cc cc"], EN, BN)
        state = {e["name"]: e for e in pipe.snapshot()["engines"]}["one"]
        self.assertEqual(state["consecutive_strikes"], 0)
        self.assertEqual(state["cooldown_remaining_seconds"], 0)
        self.assertFalse(results[0].retryable)


class Pacing(unittest.TestCase):
    def test_pauses_between_calls_to_the_same_engine(self):
        one = FakeEngine("one")
        pipe, clock, _ = make_pipeline([one])
        pipe.run_batch(["aa aa", "bb bb", "cc cc"], EN, BN)
        self.assertEqual(clock.sleeps, [1.0, 1.0])  # none before the first call

    def test_time_limit_hands_back_the_rest_as_retryable(self):
        one = FakeEngine("one")
        pipe, _, _ = make_pipeline([one], batch_deadline_seconds=5)
        texts = [f"segment number {n}" for n in range(8)]
        results, _ = pipe.run_batch(texts, EN, BN)
        done = [r for r in results if r.text is not None]
        late = [r for r in results if r.text is None]
        self.assertEqual(len(done), 6)
        self.assertTrue(all(r.retryable for r in late))
        self.assertEqual(late[0].attempts[-1]["kind"], "deadline")


class OneAtATime(unittest.TestCase):
    def test_batches_from_several_threads_never_overlap(self):
        class Probe(Engine):
            name = "probe"
            active = 0
            peak = 0
            lock = threading.Lock()

            def translate(self, text, sl, tl, timeout):
                with self.lock:
                    Probe.active += 1
                    Probe.peak = max(Probe.peak, Probe.active)
                time.sleep(0.02)
                with self.lock:
                    Probe.active -= 1
                return "x"

        pipe, _ = make_real_time_pipeline([Probe()])
        threads = [
            threading.Thread(target=pipe.run_batch, args=([f"thread {i} text {j}" for j in range(3)], EN, BN))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(Probe.peak, 1)

    def test_waiting_too_long_for_the_queue_raises_busy(self):
        slow = FakeEngine("slow", delay=0.4)
        pipe, _ = make_real_time_pipeline([slow], queue_wait_seconds=0.05)
        holder = threading.Thread(target=pipe.run_batch, args=(["hold the queue"], EN, BN))
        holder.start()
        time.sleep(0.1)
        with self.assertRaises(Busy):
            pipe.run_batch(["too late"], EN, BN)
        holder.join()

    def test_a_hung_engine_cannot_freeze_the_queue(self):
        hung = FakeEngine("hung", delay=0.4)
        with mock.patch.object(pipeline_module, "TIMEOUT_GRACE", 0.0):
            pipe, _ = make_real_time_pipeline([hung], engine_timeout_seconds=0.05, engine_retries=0)
            started = time.time()
            results, _ = pipe.run_batch(["Hello there"], EN, BN)
        self.assertLess(time.time() - started, 0.3)
        self.assertIn("Timed out", results[0].error)


class SelfTestAndStatus(unittest.TestCase):
    def test_selftest_reports_placeholder_survival(self):
        one = FakeEngine("one", ["uno", "dos {0} tres"])
        two = FakeEngine("two", ["eins", "zwei drei"])
        pipe, _, _ = make_pipeline([one, two])
        report = {r["engine"]: r for r in pipe.selftest()}
        self.assertEqual(report["one"]["status"], "ok")
        self.assertTrue(report["one"]["placeholder_kept"])
        self.assertFalse(report["two"]["placeholder_kept"])

    def test_selftest_is_rate_limited_and_respects_cooldowns(self):
        one = FakeEngine("one", [EngineError("blocked")])
        pipe, clock, _ = make_pipeline([one, FakeEngine("two")])
        pipe.run_batch(["Hello there"], EN, BN)           # trips engine one
        report = {r["engine"]: r for r in pipe.selftest()}
        self.assertEqual(report["one"]["status"], "cooldown")
        with self.assertRaises(TooSoon):
            pipe.selftest()
        clock.advance(61)
        forced = {r["engine"]: r for r in pipe.selftest(force=True)}
        self.assertEqual(forced["one"]["status"], "ok")

    def test_selftest_failure_names_the_reason(self):
        one = FakeEngine("one", [EngineError("network", "no route")] * 2)
        pipe, _, _ = make_pipeline([one])
        report = pipe.selftest()
        self.assertEqual(report[0]["status"], "failed")
        self.assertEqual(report[0]["kind"], "network")

    def test_snapshot_status_values_and_no_secrets(self):
        one = FakeEngine("one", [EngineError("error", "kaput")] * 2)
        pipe, _, _ = make_pipeline([one])
        self.assertEqual(pipe.snapshot()["engines"][0]["status"], "untested")
        pipe.run_batch(["Hello there"], EN, BN)
        self.assertEqual(pipe.snapshot()["engines"][0]["status"], "failing")
        pipe.run_batch(["Another one"], EN, BN)
        self.assertEqual(pipe.snapshot()["engines"][0]["status"], "ok")
        self.assertNotIn("test-token", json.dumps(pipe.snapshot()))


if __name__ == "__main__":
    unittest.main()
