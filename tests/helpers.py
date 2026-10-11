import time

from app.config import Settings
from app.engines.base import Engine
from app.pipeline import Pipeline

TOKEN = "test-token-0123456789"


class FakeClock:
    """Time that only moves when the code under test sleeps, so tests run instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class FakeEngine(Engine):
    """Plays back a script: a string is a translation, an exception is raised, nothing left means success."""

    def __init__(self, name, script=None, delay=0.0, languages=None):
        self.name = name
        self.script = list(script or [])
        self.calls = []
        self.delay = delay
        self.languages = languages

    def supports(self, sl, tl):
        return self.languages is None or (sl, tl) in self.languages

    def translate(self, text, sl, tl, timeout):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, str) else f"{self.name}:{text}"


def make_settings(names, **overrides):
    values = dict(
        api_token=TOKEN,
        engine_order=tuple(names),
        delays={name: (1.0, 1.0) for name in names},
        engine_timeout_seconds=15.0,
        engine_retries=1,
        retry_backoff_seconds=2.0,
        breaker_threshold=3,
        cooldown_steps=(300, 900, 3600),
        cache_size=100,
        queue_wait_seconds=1.0,
        batch_deadline_seconds=80.0,
        selftest_min_interval_seconds=60.0,
    )
    values.update(overrides)
    return Settings(**values)


def make_pipeline(engines, **overrides):
    """Pipeline on a fake clock. Returns (pipeline, clock, engines_by_name)."""
    by_name = {e.name: e for e in engines}
    clock = FakeClock()
    settings = make_settings(list(by_name), **overrides)
    pipe = Pipeline(by_name, settings, clock=clock, sleep=clock.sleep, wall=lambda: 1_700_000_000.0,
                    uniform=lambda lo, hi: lo)
    return pipe, clock, by_name


def make_real_time_pipeline(engines, **overrides):
    by_name = {e.name: e for e in engines}
    settings = make_settings(list(by_name), delays={n: (0.0, 0.0) for n in by_name}, **overrides)
    return Pipeline(by_name, settings), by_name
