"""MyMemory (free, with daily limits) through deep-translator. Used as the fallback."""
from __future__ import annotations

import re

from ..textutils import chunk_text
from .base import EMPTY, RATE_LIMITED, UNSUPPORTED, Engine, EngineError
from .errors import classify_exception

MAX_CHUNK = 450  # MyMemory refuses long queries (about 500 characters); stay under that

# MyMemory wants region-tagged codes, unlike Google. Extend as needed.
LOCALES = {
    "en": "en-GB", "bn": "bn-IN", "hi": "hi-IN", "ur": "ur-PK", "ar": "ar-SA",
    "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT", "pt": "pt-PT",
    "ru": "ru-RU", "tr": "tr-TR", "id": "id-ID", "ta": "ta-IN", "te": "te-IN",
    "mr": "mr-IN", "gu": "gu-IN", "pa": "pa-IN", "ne": "ne-NP", "nl": "nl-NL",
    "pl": "pl-PL", "vi": "vi-VN", "th": "th-TH", "ja": "ja-JP", "ko": "ko-KR",
}
_REGION_CODE = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}$")


def locale_for(code: str) -> str | None:
    if _REGION_CODE.match(code):
        return code
    return LOCALES.get(code.split("-")[0].lower())


def check_mymemory_text(out: str | None) -> str:
    """MyMemory sometimes reports problems inside the 'translation' text itself."""
    if not out or not out.strip():
        raise EngineError(EMPTY, "MyMemory returned an empty result.")
    upper = out.upper()
    if "MYMEMORY WARNING" in upper or "USED ALL AVAILABLE FREE TRANSLATIONS" in upper:
        raise EngineError(RATE_LIMITED, "MyMemory daily free limit reached.")
    if "DISTINCT LANGUAGES" in upper or "INVALID LANGUAGE PAIR" in upper:
        raise EngineError(UNSUPPORTED, "MyMemory does not support this language pair.")
    return out


class MyMemoryEngine(Engine):
    name = "mymemory"

    def supports(self, sl: str, tl: str) -> bool:
        return locale_for(sl) is not None and locale_for(tl) is not None

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        src, tgt = locale_for(sl), locale_for(tl)
        if not src or not tgt:
            raise EngineError(UNSUPPORTED, f"No MyMemory locale known for {sl!r} or {tl!r}.")
        from deep_translator import MyMemoryTranslator

        translator = MyMemoryTranslator(source=src, target=tgt)
        parts: list[str] = []
        for piece in chunk_text(text, MAX_CHUNK):
            try:
                out = translator.translate(piece)
            except Exception as exc:  # noqa: BLE001
                raise classify_exception(exc) from None
            parts.append(check_mymemory_text(out))
        return " ".join(parts).strip()
