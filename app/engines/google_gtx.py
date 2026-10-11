"""Google Translate's JSON endpoint (the one the CAT site used from the browser)."""
from __future__ import annotations

import json
from urllib.parse import quote

from .base import BLOCKED, EMPTY, ERROR, RATE_LIMITED, Engine, EngineError
from .errors import classify_exception

URL = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_GET_QUERY = 6000  # longer text goes in a POST body so the URL stays short
_BLOCK_MARKERS = ("automated queries", "we're sorry", "unusual traffic", "<html")


def parse_gtx_response(status: int, content_type: str, body: str) -> str:
    """Pure function so the tricky cases can be tested without a network."""
    lowered = body[:2000].lower()
    if status == 429:
        raise EngineError(RATE_LIMITED, "HTTP 429 from Google.")
    if "text/html" in (content_type or "").lower() or any(m in lowered for m in _BLOCK_MARKERS):
        raise EngineError(BLOCKED, "Google answered with a web page instead of data (probably a block page).")
    if status == 403:
        raise EngineError(BLOCKED, "HTTP 403 from Google.")
    if status != 200:
        raise EngineError(ERROR, f"HTTP {status} from Google.")
    try:
        data = json.loads(body)
        out = "".join(part[0] for part in data[0] if part and part[0])
    except (ValueError, TypeError, IndexError, KeyError):
        raise EngineError(ERROR, "Unexpected response format from Google.") from None
    if not out.strip():
        raise EngineError(EMPTY, "Google returned an empty result.")
    return out


class GoogleGtxEngine(Engine):
    name = "google_gtx"

    def translate(self, text: str, sl: str, tl: str, timeout: float) -> str:
        import requests

        params = {"client": "gtx", "sl": sl, "tl": tl, "dt": "t"}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            if len(quote(text)) <= MAX_GET_QUERY:
                resp = requests.get(URL, params={**params, "q": text}, headers=headers, timeout=timeout)
            else:
                resp = requests.post(URL, params=params, data={"q": text}, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        return parse_gtx_response(resp.status_code, resp.headers.get("content-type", ""), resp.text)
