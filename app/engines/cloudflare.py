"""Cloudflare Workers AI translation engine (@cf/meta/m2m100-1.2b)."""
from __future__ import annotations

import logging
import os

import requests

from ..textutils import check_tags_preserved, normalize_ascii_digits, scrub
from .base import BLOCKED, EMPTY, ERROR, NETWORK, RATE_LIMITED, UNSUPPORTED, Engine, EngineError
from .errors import classify_exception

log = logging.getLogger("cat.engine.cloudflare")

DEFAULT_CLOUDFLARE_MODEL = "@cf/meta/m2m100-1.2b"


class CloudflareEngine(Engine):
    name = "cloudflare"

    def __init__(
        self,
        account_id: str = "",
        api_token: str = "",
        model: str = DEFAULT_CLOUDFLARE_MODEL,
    ):
        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.api_token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        self.model = model or os.environ.get("CLOUDFLARE_MODEL", DEFAULT_CLOUDFLARE_MODEL)

    def supports(self, sl: str, tl: str) -> bool:
        # Requires both account_id and api_token to be available
        return bool(self.account_id.strip() and self.api_token.strip())

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        if not self.supports(sl, tl):
            raise EngineError(UNSUPPORTED, "Cloudflare credentials (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN) not configured.")

        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{self.model}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "source_lang": sl,
            "target_lang": tl,
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.Timeout:
            raise EngineError(NETWORK, f"Cloudflare timed out after {timeout:.0f}s.") from None
        except requests.exceptions.RequestException as exc:
            raise classify_exception(exc) from None

        if resp.status_code in (401, 403):
            raise EngineError(BLOCKED, f"Cloudflare auth rejected (HTTP {resp.status_code}).")
        if resp.status_code == 429:
            raise EngineError(RATE_LIMITED, "Cloudflare rate limit exceeded (HTTP 429).")
        if resp.status_code >= 500:
            raise EngineError(ERROR, f"Cloudflare server error (HTTP {resp.status_code}).")
        if resp.status_code != 200:
            raise EngineError(ERROR, f"Cloudflare HTTP {resp.status_code}: {scrub(resp.text, text)}")

        try:
            data = resp.json()
        except Exception:
            raise EngineError(EMPTY, "Cloudflare returned invalid JSON.") from None

        result = data.get("result", {})
        translated = result.get("translated_text", "")
        if not translated or not str(translated).strip():
            raise EngineError(EMPTY, "Cloudflare returned an empty translation.")

        normalized = normalize_ascii_digits(str(translated).strip())

        # Validate tag preservation
        tags_ok, missing_tag = check_tags_preserved(text, normalized)
        if not tags_ok:
            raise EngineError(EMPTY, f"Cloudflare lost tag '{missing_tag}' in translation.")

        return normalized
