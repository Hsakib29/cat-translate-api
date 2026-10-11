"""Turn ALLOWED_ORIGINS into what the CORS middleware needs."""
from __future__ import annotations

import re

from .config import ConfigError

_LOCALHOST = r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?"
_WILDCARD_LABELS = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"


def build_cors(allowed: tuple[str, ...], allow_localhost: bool) -> tuple[list[str], str | None]:
    """Exact origins go in a list; entries with '*' (e.g. https://*.lovable.app) become one regex."""
    exact: list[str] = []
    patterns: list[str] = []
    for raw in allowed:
        origin = raw.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ConfigError("ALLOWED_ORIGINS must list real site addresses, not '*'.")
        if not re.match(r"^https?://", origin):
            raise ConfigError(f"ALLOWED_ORIGINS entry {raw!r} must start with http:// or https://.")
        if "*" in origin:
            patterns.append(re.escape(origin).replace(r"\*", _WILDCARD_LABELS))
        else:
            exact.append(origin)
    if allow_localhost:
        patterns.append(_LOCALHOST)
    regex = "(?:" + "|".join(patterns) + ")" if patterns else None
    return exact, regex
