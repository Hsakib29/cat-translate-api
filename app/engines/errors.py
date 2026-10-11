"""Turn exceptions from the libraries we use into EngineError kinds."""
from __future__ import annotations

from .base import BAD_REQUEST, EMPTY, ERROR, NETWORK, RATE_LIMITED, UNSUPPORTED, EngineError

_NETWORK = {
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "ProxyError", "ChunkedEncodingError", "TimeoutError", "OSError",
}
_EMPTY = {"TranslationNotFound", "ElementNotFoundInGetRequest"}
_BAD = {"NotValidPayload", "NotValidLength"}


def classify_exception(exc: BaseException) -> EngineError:
    """Match on class names so we don't need to import every library here."""
    if isinstance(exc, EngineError):
        return exc
    names = {cls.__name__ for cls in type(exc).__mro__}
    detail = f"{type(exc).__name__}: {exc}"
    if "TooManyRequests" in names:
        return EngineError(RATE_LIMITED, detail)
    if names & _EMPTY:
        return EngineError(EMPTY, detail)
    if "LanguageNotSupportedException" in names:
        return EngineError(UNSUPPORTED, detail)
    if names & _BAD:
        return EngineError(BAD_REQUEST, detail)
    if names & _NETWORK:
        return EngineError(NETWORK, detail)
    return EngineError(ERROR, detail)
