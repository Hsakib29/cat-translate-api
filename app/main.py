"""HTTP layer. Thin on purpose: the logic lives in pipeline.py."""
from __future__ import annotations

import logging
import secrets
import socket

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import __version__
from .config import Settings
from .cors import build_cors
from .engines import build_engines
from .pipeline import Busy, Pipeline, TooSoon, UnknownEngine
from .validation import RequestError, validate_langs, validate_translate

log = logging.getLogger("cat.api")


class TranslateRequest(BaseModel):
    sl: str = "en"
    tl: str = "bn"
    segments: list[str]
    engines: list[str] | None = None


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    socket.setdefaulttimeout(settings.engine_timeout_seconds + 10)  # safety net for libraries with no timeout
    pipe = pipeline or Pipeline(build_engines(), settings)

    app = FastAPI(
        title="CAT Translate API",
        version=__version__,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    origins, origin_regex = build_cors(settings.allowed_origins, settings.allow_localhost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    def require_token(request: Request) -> None:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        good = scheme.lower() == "bearer" and secrets.compare_digest(
            supplied.strip().encode(), settings.api_token.encode()
        )
        if not good:
            raise HTTPException(401, "Invalid or missing token.", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "cat-translate-api", "version": __version__}

    @app.get("/api/engines", dependencies=[Depends(require_token)])
    def engines():
        return pipe.snapshot()

    @app.get("/api/selftest", dependencies=[Depends(require_token)])
    def selftest(sl: str = "en", tl: str = "bn", force: bool = False):
        try:
            validate_langs(sl, tl)
            return {"results": pipe.selftest(sl, tl, force)}
        except RequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except TooSoon as exc:
            raise HTTPException(
                429, f"Self-test ran recently. Try again in {exc.retry_after}s.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        except Busy:
            raise HTTPException(503, "Busy with another request. Try again shortly.", headers={"Retry-After": "5"}) from None

    @app.post("/api/translate", dependencies=[Depends(require_token)])
    def translate(body: TranslateRequest):
        try:
            validate_translate(settings, body.sl, body.tl, body.segments)
            results, meta = pipe.run_batch(body.segments, body.sl, body.tl, body.engines)
        except RequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except UnknownEngine as exc:
            raise HTTPException(400, str(exc)) from None
        except Busy:
            raise HTTPException(503, "Busy with another request. Try again shortly.", headers={"Retry-After": "5"}) from None
        failed = sum(1 for r in results if r.text is None)
        # Never log the text itself, only counts and timings.
        log.info(
            "translate segments=%d chars=%d failed=%d ms=%d queue_ms=%d",
            len(results), sum(len(s) for s in body.segments), failed, meta["elapsed_ms"], meta["queue_wait_ms"],
        )
        return {"results": [r.as_dict() for r in results], "meta": meta}

    return app
