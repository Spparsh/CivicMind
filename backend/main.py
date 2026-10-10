"""
CivicMind — FastAPI application entry point.

Run locally:   cd backend && python -m uvicorn main:app --port 8000
Open:          http://localhost:8000
"""
import logging
import os
import re
import sys
import traceback
import uuid
from contextlib import asynccontextmanager

# Make `from services import ...` work no matter where uvicorn is launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import config  # noqa: E402
from config import FRONTEND_DIR, CITY_NAME, CITY_CENTER, CORS_ORIGINS, MAX_REQUEST_BYTES  # noqa: E402
from database import init_db, query, is_demo_db  # noqa: E402
from services import bootstrap, ai, voice, media  # noqa: E402
from services.tinyfish import provider_status  # noqa: E402
from routes import (auth_routes, complaints, incidents, actions, dashboard, admin,  # noqa: E402
                    realtime, config_routes, infrastructure, data_sources, assistant, coordination,
                    utility_network, workorders, voice_geo, media as media_routes)

log = logging.getLogger("civicmind")


class _RedactTokens(logging.Filter):
    """Keep SSE tokens (passed in the query string) out of access logs."""
    _re = re.compile(r"(token=)[^&\s\"]+")

    def filter(self, record):
        if record.args:
            record.args = tuple(self._re.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokens())


@asynccontextmanager
async def lifespan(_app):
    problems = config.production_problems()
    if problems:   # CIVICMIND_ENV=production refuses to start in an unsafe configuration
        raise RuntimeError("CivicMind will not start in production: " + " ".join(problems))
    init_db()
    if config.IS_PRODUCTION and is_demo_db():
        raise RuntimeError("CivicMind will not start in production on a database marked as a DEMO database.")
    result = bootstrap.bootstrap()
    voice.purge()  # retention policy for voice recordings
    media.purge()  # uploads never attached to a report
    print(f"[CivicMind] Environment: {config.ENVIRONMENT}")
    print(f"[CivicMind] Database: {config.DB_PATH}{'  (DEMO database)' if is_demo_db() else ''}")
    print(f"[CivicMind] Bootstrap: {result}")
    print("[CivicMind] AI engine: " + (f"Sarvam AI ({ai.SARVAM_CHAT_MODEL}) with rule-based fallback"
                                       if ai.ai_available() else "rule-based (set SARVAM_API_KEY to enable Sarvam AI)"))
    tf = provider_status()
    print("[CivicMind] TinyFish: " + {"live": "configured (live)", "mock": "MOCK MODE (fictional responses)"}.get(
        tf["provider_mode"], "not configured (set TINYFISH_API_KEY for data-source discovery)")
        + ("; Agent enabled" if tf["agent_enabled"] else ""))
    yield


app = FastAPI(title="CivicMind API", version="3.0.0", lifespan=lifespan,
              description="Civic complaint intelligence: complaints -> incidents -> infrastructure risk -> verified action.")

if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"])

CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
       "font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://unpkg.com; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # OpenStreetMap refuses tile requests without a Referer; send only the origin
    # (never paths or query strings) to other sites.
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"geolocation=(self), camera=(self), microphone=(self)"),
    (b"content-security-policy", CSP.encode()),
]


class GuardMiddleware:
    """Pure-ASGI: request-size limit + security headers (works with SSE)."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        try:
            declared = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            declared = 0
        # Photo/video uploads are raw bytes on one route with its own (larger) cap.
        limit = media_routes.UPLOAD_LIMIT if scope["path"] == media_routes.UPLOAD_PATH else MAX_REQUEST_BYTES
        if declared > limit:
            return await _too_large(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge()
            return msg

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                hdrs = list(msg.get("headers", []))
                names = {h[0].lower() for h in hdrs}
                hdrs += [h for h in SECURITY_HEADERS if h[0] not in names]
                if scope.get("scheme") == "https" and b"strict-transport-security" not in names:
                    # Served over HTTPS (directly or via a trusted proxy): keep browsers on HTTPS.
                    hdrs.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                path = scope["path"]
                if b"cache-control" not in names:
                    if path.startswith("/api/"):
                        hdrs.append((b"cache-control", b"no-store"))
                    elif path == "/" or path.endswith((".html", ".js", ".css")):
                        # Revalidate (ETag) so a browser never runs stale pages after an update.
                        hdrs.append((b"cache-control", b"no-cache"))
                msg["headers"] = hdrs
            await send(msg)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    body = b'{"detail":"Request is too large."}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


app.add_middleware(GuardMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Field-level messages only — never echo the submitted values (they may be large images).
    problems = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(p) for p in e.get("loc", []) if p != "body")
        problems.append(f"{loc}: {e.get('msg', 'invalid')}" if loc else e.get("msg", "invalid"))
    return JSONResponse(status_code=422, content={"detail": {"message": "Invalid request.", "reasons": problems}})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    ref = uuid.uuid4().hex[:10]
    log.error("Unhandled error %s on %s %s\n%s", ref, request.method, request.url.path,
              "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return JSONResponse(status_code=500,
                        content={"detail": f"Something went wrong on the server (reference {ref})."})


@app.get("/api/health")
def health():
    return {
        "status": "ok", "city": CITY_NAME, "center": CITY_CENTER,
        "ai_enabled": ai.ai_available(),
        "ai_model": ai.SARVAM_CHAT_MODEL if ai.ai_available() else None,
        "complaints": query("SELECT COUNT(*) c FROM complaints", one=True)["c"],
        "incidents": query("SELECT COUNT(*) c FROM incidents", one=True)["c"],
        "assets": query("SELECT COUNT(*) c FROM assets", one=True)["c"],
        "demo_database": is_demo_db(),
    }


for r in (config_routes, auth_routes, complaints, incidents, actions, dashboard, admin,
          realtime, infrastructure, data_sources, assistant, coordination, utility_network, workorders, voice_geo, media_routes):
    app.include_router(r.router)

# Short portal entry points. They only pick a page; every API call behind the
# page is still authorised on the server from the user's current role.
PORTAL_PATHS = {"/citizen": "/citizen.html", "/officer": "/dashboard.html", "/admin": "/admin.html",
                "/login": "/index.html#login"}


def _portal_redirect(target):
    return lambda: RedirectResponse(target, status_code=307)


for _path, _target in PORTAL_PATHS.items():
    app.add_api_route(_path, _portal_redirect(_target), methods=["GET"], include_in_schema=False)

# Frontend last so /api/* wins. NOTE: evidence photos are NOT served statically.
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
