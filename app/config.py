"""Settings, read once from environment variables at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

MIN_TOKEN_LENGTH = 16
DEFAULT_ORDER = ("google_web", "mymemory", "google_gtx")
DEFAULT_DELAYS = {
    "google_web": (0.6, 1.4),
    "google_gtx": (0.6, 1.4),
    "mymemory": (0.5, 1.0),
}
FALLBACK_DELAY = (0.6, 1.4)


class ConfigError(RuntimeError):
    """The environment is unusable. The message says what to fix."""


def _number(env: Mapping[str, str], name: str, default, cast, lo, hi):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if not (lo <= value <= hi):
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}.")
    return value


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}.")


def _names(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_delays(raw: str | None) -> dict[str, tuple[float, float]]:
    """'google_web:0.6-1.4,mymemory:0.5-1' -> per-engine (min, max) pause in seconds."""
    delays = dict(DEFAULT_DELAYS)
    for item in _names(raw):
        try:
            name, rng = item.split(":", 1)
            lo_s, hi_s = rng.split("-", 1)
            lo, hi = float(lo_s), float(hi_s)
        except ValueError:
            raise ConfigError(
                f"ENGINE_DELAYS entry {item!r} must look like name:0.6-1.4."
            ) from None
        if not (0 <= lo <= hi <= 60):
            raise ConfigError(f"ENGINE_DELAYS entry {item!r} needs 0 <= min <= max <= 60.")
        delays[name.strip()] = (lo, hi)
    return delays


def parse_steps(raw: str | None) -> tuple[int, ...]:
    """'300,900,3600' -> cooldown lengths in seconds (each trip uses the next one)."""
    if not raw or not raw.strip():
        return (300, 900, 3600)
    try:
        steps = tuple(int(part) for part in _names(raw))
    except ValueError:
        raise ConfigError(f"COOLDOWN_STEPS must be whole seconds like 300,900,3600; got {raw!r}.") from None
    if not steps or any(s < 1 or s > 86400 for s in steps):
        raise ConfigError("COOLDOWN_STEPS values must be between 1 and 86400 seconds.")
    return steps


@dataclass(frozen=True)
class Settings:
    api_token: str
    allowed_origins: tuple[str, ...] = ()
    allow_localhost: bool = True
    enable_docs: bool = False
    engine_order: tuple[str, ...] = DEFAULT_ORDER
    delays: Mapping[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_DELAYS))
    engine_timeout_seconds: float = 15.0
    engine_retries: int = 1
    retry_backoff_seconds: float = 2.0
    breaker_threshold: int = 3
    cooldown_steps: tuple[int, ...] = (300, 900, 3600)
    cache_size: int = 2000
    max_segments: int = 10
    max_segment_chars: int = 4500
    max_request_chars: int = 8000
    queue_wait_seconds: float = 120.0
    batch_deadline_seconds: float = 80.0
    selftest_min_interval_seconds: float = 60.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        token = (env.get("API_TOKEN") or "").strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"API_TOKEN must be set to a long random secret (at least {MIN_TOKEN_LENGTH} characters)."
            )
        order = _names(env.get("ENGINE_ORDER")) or DEFAULT_ORDER
        level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {level!r}.")
        return cls(
            api_token=token,
            allowed_origins=_names(env.get("ALLOWED_ORIGINS")),
            allow_localhost=_flag(env, "ALLOW_LOCALHOST", True),
            enable_docs=_flag(env, "ENABLE_DOCS", False),
            engine_order=tuple(order),
            delays=parse_delays(env.get("ENGINE_DELAYS")),
            engine_timeout_seconds=_number(env, "ENGINE_TIMEOUT_SECONDS", 15.0, float, 1, 120),
            engine_retries=_number(env, "ENGINE_RETRIES", 1, int, 0, 5),
            retry_backoff_seconds=_number(env, "RETRY_BACKOFF_SECONDS", 2.0, float, 0, 30),
            breaker_threshold=_number(env, "BREAKER_THRESHOLD", 3, int, 1, 20),
            cooldown_steps=parse_steps(env.get("COOLDOWN_STEPS")),
            cache_size=_number(env, "CACHE_SIZE", 2000, int, 0, 100000),
            max_segments=_number(env, "MAX_SEGMENTS", 10, int, 1, 50),
            max_segment_chars=_number(env, "MAX_SEGMENT_CHARS", 4500, int, 100, 20000),
            max_request_chars=_number(env, "MAX_REQUEST_CHARS", 8000, int, 100, 100000),
            queue_wait_seconds=_number(env, "QUEUE_WAIT_SECONDS", 120.0, float, 0, 600),
            batch_deadline_seconds=_number(env, "BATCH_DEADLINE_SECONDS", 80.0, float, 5, 600),
            selftest_min_interval_seconds=_number(env, "SELFTEST_MIN_INTERVAL_SECONDS", 60.0, float, 0, 3600),
            log_level=level,
        )
