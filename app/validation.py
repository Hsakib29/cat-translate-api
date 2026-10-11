"""Checks on incoming requests, kept free of web-framework code so they are easy to test."""
from __future__ import annotations

import re

from .config import Settings

LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate_langs(sl: str, tl: str) -> None:
    if not LANG_CODE.match(sl or "") or not LANG_CODE.match(tl or ""):
        raise RequestError(400, "Language codes must look like 'en', 'bn' or 'zh-CN'.")
    if sl.lower() == tl.lower():
        raise RequestError(400, "Source and target language are the same.")


def validate_translate(settings: Settings, sl: str, tl: str, segments: list[str]) -> None:
    validate_langs(sl, tl)
    if not segments:
        raise RequestError(400, "No segments to translate.")
    if len(segments) > settings.max_segments:
        raise RequestError(413, f"At most {settings.max_segments} segments per request.")
    if any(len(s) > settings.max_segment_chars for s in segments):
        raise RequestError(413, f"A segment is longer than {settings.max_segment_chars} characters.")
    if sum(len(s) for s in segments) > settings.max_request_chars:
        raise RequestError(413, f"At most {settings.max_request_chars} characters per request.")
