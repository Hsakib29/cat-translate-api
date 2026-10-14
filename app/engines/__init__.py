"""Registry of available engines. To add one: write a class, import it here, list it in ENGINE_CLASSES."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Engine
from .cloudflare import CloudflareEngine
from .gemini import GeminiEngine
from .google_gtx import GoogleGtxEngine
from .google_web import GoogleWebEngine
from .mymemory import MyMemoryEngine

if TYPE_CHECKING:
    from ..config import Settings

ENGINE_CLASSES = (GeminiEngine, CloudflareEngine, MyMemoryEngine, GoogleWebEngine, GoogleGtxEngine)


def build_engines(settings: Settings | None = None) -> dict[str, Engine]:
    if settings is None:
        return {
            "gemini": GeminiEngine(),
            "cloudflare": CloudflareEngine(),
            "mymemory": MyMemoryEngine(),
            "google_web": GoogleWebEngine(),
            "google_gtx": GoogleGtxEngine(),
        }
    return {
        "gemini": GeminiEngine(
            api_key=settings.gemini_api_key,
            models=settings.gemini_models,
            delays=settings.delays.get("gemini", (4.0, 5.0)),
        ),
        "cloudflare": CloudflareEngine(
            account_id=settings.cloudflare_account_id,
            api_token=settings.cloudflare_api_token,
            model=settings.cloudflare_model,
        ),
        "mymemory": MyMemoryEngine(email=settings.mymemory_email),
        "google_web": GoogleWebEngine(),
        "google_gtx": GoogleGtxEngine(),
    }
