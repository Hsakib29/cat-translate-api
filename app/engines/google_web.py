"""Google Translate through the deep-translator library (the same one your Render backend uses)."""
from __future__ import annotations

from .base import EMPTY, Engine, EngineError
from .errors import classify_exception


class GoogleWebEngine(Engine):
    name = "google_web"

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        from deep_translator import GoogleTranslator  # imported here so tests don't need the library

        try:
            out = GoogleTranslator(source=sl, target=tl).translate(text)
        except Exception as exc:  # noqa: BLE001 - classified below
            raise classify_exception(exc) from None
        if not out or not str(out).strip():
            raise EngineError(EMPTY, "Google returned an empty result.")
        return str(out)
