"""Registry of available engines. To add one: write a class, import it here, list it in ENGINE_CLASSES."""
from __future__ import annotations

from .base import Engine
from .google_gtx import GoogleGtxEngine
from .google_web import GoogleWebEngine
from .mymemory import MyMemoryEngine

ENGINE_CLASSES = (GoogleWebEngine, MyMemoryEngine, GoogleGtxEngine)


def build_engines() -> dict[str, Engine]:
    return {cls.name: cls() for cls in ENGINE_CLASSES}
