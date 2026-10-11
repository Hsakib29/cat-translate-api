"""The contract every translation engine follows."""
from __future__ import annotations

# What went wrong, in terms the pipeline understands.
BLOCKED = "blocked"            # the provider answered with a "you look like a robot" page
RATE_LIMITED = "rate_limited"  # HTTP 429 or a quota message
NETWORK = "network"            # timeout, DNS, connection reset
EMPTY = "empty"                # the provider answered but gave no translation
ERROR = "error"                # anything else unexpected
UNSUPPORTED = "unsupported"    # this engine can't do this language pair
BAD_REQUEST = "bad_request"    # the input itself was refused (too long, invalid)

# Failures that count against an engine and can put it into a cooldown.
STRIKE_KINDS = frozenset({BLOCKED, RATE_LIMITED, NETWORK, EMPTY, ERROR})
# A single one of these is enough to stop using the engine for a while.
TRIP_NOW_KINDS = frozenset({BLOCKED, RATE_LIMITED})
# Worth trying once more on the same engine before moving to the next one.
RETRY_KINDS = frozenset({NETWORK, ERROR})


class EngineError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.message = message or kind


class Engine:
    """Subclass this, set `name`, implement `translate`, then register it in engines/__init__.py."""

    name = ""

    def supports(self, sl: str, tl: str) -> bool:
        return True

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        """Return the translation or raise EngineError. Must not log or echo `text`."""
        raise NotImplementedError
