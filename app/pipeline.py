"""The heart of the service: one request at a time, a fallback chain, cooldowns and a small cache."""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import FALLBACK_DELAY, Settings
from .engines.base import (
    BAD_REQUEST, RETRY_KINDS, STRIKE_KINDS, TRIP_NOW_KINDS, UNSUPPORTED, Engine, EngineError,
)
from .engines.errors import classify_exception
from .textutils import has_letters, scrub

log = logging.getLogger("cat.pipeline")

TIMEOUT_GRACE = 2.0  # extra seconds on top of an engine's own timeout before we give up on its thread
SELFTEST_SAMPLES = ("Hello, how are you today?", "Please read {0} carefully before signing.")
_PLACEHOLDER = re.compile(r"\{\s*[0-9\u09e6-\u09ef]+\s*\}")  # {0}, { 0 } or Bengali digits


class Busy(Exception):
    """Another request held the queue for longer than QUEUE_WAIT_SECONDS."""


class TooSoon(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"try again in {retry_after}s")
        self.retry_after = retry_after


class UnknownEngine(ValueError):
    pass


@dataclass
class Result:
    text: str | None
    error: str | None = None
    via: str | None = None
    cached: bool = False
    retryable: bool = False
    attempts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EngineState:
    strikes: int = 0
    trips: int = 0
    cooldown_until: float = 0.0
    last_end: float = -1e9
    calls: int = 0
    successes: int = 0
    failures: int = 0
    last_ms: int | None = None
    last_error: dict | None = None
    last_error_at: float | None = None
    last_success_at: float | None = None


def call_with_timeout(fn: Callable[[], str], timeout: float) -> str:
    """Run fn in a helper thread so a hung network call can never freeze the whole queue."""
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise EngineError("network", f"Timed out after {timeout:.0f}s.")
    if "error" in box:
        raise box["error"]
    return box["value"]


class Pipeline:
    def __init__(
        self,
        engines: dict[str, Engine],
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ):
        unknown = [name for name in settings.engine_order if name not in engines]
        if unknown:
            raise UnknownEngine(f"ENGINE_ORDER lists unknown engines: {', '.join(unknown)}")
        self.engines = engines
        self.s = settings
        self.clock, self.wall, self.sleep, self.uniform = clock, wall, sleep, uniform
        self.states = {name: EngineState() for name in engines}
        self._queue = threading.Lock()        # held for a whole batch: one request at a time
        self._state_lock = threading.RLock()  # guards states and cache (short, never held during a call)
        self._cache: OrderedDict[tuple[str, str, str], tuple[str, str]] = OrderedDict()
        self._last_selftest: float | None = None

    # ---------- public API ----------
    def resolve_chain(self, requested: list[str] | None) -> list[str]:
        names = list(requested) if requested else list(self.s.engine_order)
        seen: list[str] = []
        for name in names:
            if name not in self.engines:
                raise UnknownEngine(f"Unknown engine {name!r}. Available: {', '.join(self.engines)}.")
            if name not in seen:
                seen.append(name)
        return seen

    def run_batch(self, segments: list[str], sl: str, tl: str, chain: list[str] | None = None):
        names = self.resolve_chain(chain)
        started = self.clock()
        if not self._queue.acquire(timeout=self.s.queue_wait_seconds):
            raise Busy()
        waited_ms = int((self.clock() - started) * 1000)
        try:
            deadline = self.clock() + self.s.batch_deadline_seconds
            results = self._translate_batch(segments, sl, tl, names, deadline)
        finally:
            self._queue.release()
        meta = {
            "elapsed_ms": int((self.clock() - started) * 1000),
            "queue_wait_ms": waited_ms,
            "engines": names,
        }
        return results, meta

    def _translate_batch(
        self, segments: list[str], sl: str, tl: str, chain: list[str], deadline: float
    ) -> list[Result]:
        if not segments:
            return []

        results: list[Result | None] = [None] * len(segments)
        uncached_indices: list[int] = []

        for i, text in enumerate(segments):
            if not text.strip():
                results[i] = Result(text=text, via="passthrough")
                continue
            if not has_letters(text):
                results[i] = Result(text=text, via="passthrough")
                continue
            hit = self._cache_get(sl, tl, text)
            if hit:
                results[i] = Result(text=hit[0], via=hit[1], cached=True)
                continue
            uncached_indices.append(i)

        if not uncached_indices:
            return [r for r in results if r is not None]

        # Context-aware batch translation: if primary engine in chain supports batching (e.g. Gemini),
        # translate all uncached segments together in a single contextual call.
        if len(uncached_indices) > 1 and chain:
            primary_name = chain[0]
            engine = self.engines[primary_name]
            can_batch = (
                hasattr(engine, "translate_batch")
                and callable(getattr(engine, "translate_batch"))
                and self._cooldown_remaining(primary_name) == 0
                and engine.supports(sl, tl)
                and self.clock() < deadline
            )
            if can_batch:
                uncached_texts = [segments[idx] for idx in uncached_indices]
                self._pace(primary_name)
                with self._state_lock:
                    self.states[primary_name].calls += 1
                batch_start = self.clock()
                try:
                    batch_outputs = call_with_timeout(
                        lambda: engine.translate_batch(
                            uncached_texts, sl, tl, self.s.engine_timeout_seconds
                        ),
                        self.s.engine_timeout_seconds + TIMEOUT_GRACE,
                    )
                    if isinstance(batch_outputs, list) and len(batch_outputs) == len(uncached_texts):
                        self._record_success(primary_name, int((self.clock() - batch_start) * 1000))
                        for idx, trans in zip(uncached_indices, batch_outputs):
                            src = segments[idx]
                            self._cache_put(sl, tl, src, trans, primary_name)
                            results[idx] = Result(text=trans, via=primary_name)
                        with self._state_lock:
                            self.states[primary_name].last_end = self.clock()
                except BaseException as exc:
                    err = classify_exception(exc)
                    self._record_failure(
                        primary_name, err, uncached_texts[0] if uncached_texts else ""
                    )
                    with self._state_lock:
                        self.states[primary_name].last_end = self.clock()

        # For any segment not batch-translated (or on batch fallback), resolve via individual fallback chain
        for idx in range(len(segments)):
            if results[idx] is None:
                results[idx] = self._translate_one(segments[idx], sl, tl, chain, deadline)

        return [r for r in results if r is not None]

    def selftest(self, sl: str = "en", tl: str = "bn", force: bool = False) -> list[dict]:
        now = self.clock()
        gap = self.s.selftest_min_interval_seconds
        if self._last_selftest is not None and now - self._last_selftest < gap:
            raise TooSoon(int(gap - (now - self._last_selftest)) + 1)
        self._last_selftest = now
        if not self._queue.acquire(timeout=self.s.queue_wait_seconds):
            raise Busy()
        try:
            deadline = self.clock() + self.s.batch_deadline_seconds
            return [self._selftest_engine(name, sl, tl, force, deadline) for name in self.engines]
        finally:
            self._queue.release()

    def snapshot(self) -> dict:
        now = self.clock()
        with self._state_lock:
            engines = []
            for name, engine in self.engines.items():
                st = self.states[name]
                remaining = max(0, int(st.cooldown_until - now) + 1) if st.cooldown_until > now else 0
                if remaining:
                    status = "cooldown"
                elif st.calls == 0:
                    status = "untested"
                elif st.last_error_at and (st.last_success_at or 0) < st.last_error_at:
                    status = "failing"
                else:
                    status = "ok"
                order = self.s.engine_order
                engines.append({
                    "name": name,
                    "status": status,
                    "position_in_default_chain": order.index(name) + 1 if name in order else None,
                    "cooldown_remaining_seconds": remaining,
                    "consecutive_strikes": st.strikes,
                    "times_put_in_cooldown": st.trips,
                    "calls": st.calls,
                    "successes": st.successes,
                    "failures": st.failures,
                    "last_call_ms": st.last_ms,
                    "last_error": st.last_error,
                    "last_error_at": st.last_error_at,
                    "last_success_at": st.last_success_at,
                    "pause_between_calls_seconds": list(self.s.delays.get(name, FALLBACK_DELAY)),
                })
            cache_entries = len(self._cache)
        return {
            "engines": engines,
            "default_chain": list(self.s.engine_order),
            "settings": {
                "engine_timeout_seconds": self.s.engine_timeout_seconds,
                "engine_retries": self.s.engine_retries,
                "breaker_threshold": self.s.breaker_threshold,
                "cooldown_steps_seconds": list(self.s.cooldown_steps),
                "max_segments": self.s.max_segments,
                "max_segment_chars": self.s.max_segment_chars,
                "max_request_chars": self.s.max_request_chars,
                "batch_deadline_seconds": self.s.batch_deadline_seconds,
                "cache_size": self.s.cache_size,
                "cache_entries": cache_entries,
            },
        }

    # ---------- one segment ----------
    def _translate_one(self, text: str, sl: str, tl: str, chain: list[str], deadline: float) -> Result:
        if not text.strip():
            return Result(text=text, via="passthrough")
        if not has_letters(text):  # numbers and punctuation need no translation (and engines reject them)
            return Result(text=text, via="passthrough")
        hit = self._cache_get(sl, tl, text)
        if hit:
            return Result(text=hit[0], via=hit[1], cached=True)

        attempts: list[dict] = []
        for name in chain:
            engine = self.engines[name]
            cooling = self._cooldown_remaining(name)
            if cooling:
                attempts.append({"engine": name, "kind": "cooldown", "message": f"cooling down for {cooling}s more"})
                continue
            if not engine.supports(sl, tl):
                attempts.append({"engine": name, "kind": UNSUPPORTED, "message": f"does not support {sl}->{tl}"})
                continue
            if self.clock() >= deadline:
                attempts.append({"engine": name, "kind": "deadline", "message": "time limit for this request reached"})
                break
            out, err = self._call_engine(name, text, sl, tl, deadline)
            if err is None:
                self._cache_put(sl, tl, text, out, name)
                return Result(text=out, via=name, attempts=attempts)
            attempts.append({"engine": name, "kind": err.kind, "message": scrub(err.message, text)})

        summary = "; ".join(f"{a['engine']}: {a['message']}" for a in attempts) or "no engine available"
        retryable = any(a["kind"] not in (UNSUPPORTED, BAD_REQUEST) for a in attempts)
        return Result(text=None, error=summary, retryable=retryable, attempts=attempts)

    # ---------- one engine ----------
    def _call_engine(self, name: str, text: str, sl: str, tl: str, deadline: float):
        engine = self.engines[name]
        allowed = 1 + max(0, self.s.engine_retries)
        error: EngineError | None = None
        for attempt in range(1, allowed + 1):
            if self.clock() >= deadline:
                return None, EngineError("deadline", "time limit for this request reached")
            self._pace(name)
            started = self.clock()
            with self._state_lock:
                self.states[name].calls += 1
            try:
                out = call_with_timeout(
                    lambda: engine.translate(text, sl, tl, self.s.engine_timeout_seconds),
                    self.s.engine_timeout_seconds + TIMEOUT_GRACE,
                )
                if not isinstance(out, str) or not out.strip():
                    raise EngineError("empty", "Engine returned an empty result.")
                self._record_success(name, int((self.clock() - started) * 1000))
                return out, None
            except BaseException as exc:  # noqa: BLE001 - everything becomes an EngineError
                error = classify_exception(exc)
            finally:
                with self._state_lock:
                    self.states[name].last_end = self.clock()
            if error.kind in RETRY_KINDS and attempt < allowed:
                self.sleep(self.s.retry_backoff_seconds * attempt)
                continue
            break
        self._record_failure(name, error, text)
        return None, error

    def _pace(self, name: str) -> None:
        lo, hi = self.s.delays.get(name, FALLBACK_DELAY)
        gap = self.uniform(lo, hi)
        with self._state_lock:
            elapsed = self.clock() - self.states[name].last_end
        if elapsed < gap:
            self.sleep(gap - elapsed)

    # ---------- bookkeeping ----------
    def _cooldown_remaining(self, name: str) -> int:
        with self._state_lock:
            left = self.states[name].cooldown_until - self.clock()
        return int(left) + 1 if left > 0 else 0

    def _record_success(self, name: str, ms: int) -> None:
        with self._state_lock:
            st = self.states[name]
            st.successes += 1
            st.strikes = 0
            st.trips = 0
            st.cooldown_until = 0.0
            st.last_ms = ms
            st.last_success_at = self.wall()

    def _record_failure(self, name: str, error: EngineError, text: str) -> None:
        with self._state_lock:
            st = self.states[name]
            st.failures += 1
            st.last_error = {"kind": error.kind, "message": scrub(error.message, text)}
            st.last_error_at = self.wall()
            if error.kind not in STRIKE_KINDS:
                return
            st.strikes += 1
            if error.kind in TRIP_NOW_KINDS or st.strikes >= self.s.breaker_threshold:
                steps = self.s.cooldown_steps
                seconds = steps[min(st.trips, len(steps) - 1)]
                st.cooldown_until = self.clock() + seconds
                st.trips += 1
                st.strikes = 0
                log.warning("engine %s cooling down for %ss (%s)", name, seconds, error.kind)

    def _cache_get(self, sl: str, tl: str, text: str):
        if self.s.cache_size <= 0:
            return None
        key = (sl.lower(), tl.lower(), text)
        with self._state_lock:
            hit = self._cache.get(key)
            if hit:
                self._cache.move_to_end(key)
            return hit

    def _cache_put(self, sl: str, tl: str, text: str, out: str, via: str) -> None:
        if self.s.cache_size <= 0:
            return
        with self._state_lock:
            self._cache[(sl.lower(), tl.lower(), text)] = (out, via)
            while len(self._cache) > self.s.cache_size:
                self._cache.popitem(last=False)

    # ---------- self test ----------
    def _selftest_engine(self, name: str, sl: str, tl: str, force: bool, deadline: float) -> dict:
        engine = self.engines[name]
        cooling = self._cooldown_remaining(name)
        if cooling and not force:
            return {"engine": name, "status": "cooldown", "cooldown_remaining_seconds": cooling}
        if not engine.supports(sl, tl):
            return {"engine": name, "status": "unsupported", "message": f"does not support {sl}->{tl}"}
        started = self.clock()
        outputs: list[str] = []
        for sample in SELFTEST_SAMPLES:
            out, err = self._call_engine(name, sample, sl, tl, deadline)
            if err is not None:
                return {
                    "engine": name, "status": "failed", "kind": err.kind,
                    "message": scrub(err.message, sample),
                    "ms": int((self.clock() - started) * 1000),
                }
            outputs.append(out)
        return {
            "engine": name,
            "status": "ok",
            "ms": int((self.clock() - started) * 1000),
            "sample_output": outputs[0],
            "placeholder_kept": bool(_PLACEHOLDER.search(outputs[1])),
        }
