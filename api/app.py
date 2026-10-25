"""
ProToIt API Layer — FastAPI Application Entry Point
=====================================================
Start (from repo root): uvicorn api.app:app --host 0.0.0.0 --port 8000
"""

from pathlib import Path
import logging
import sys

# Allow `from core import ...` / `from sanitize import ...` whether this is run
# as `uvicorn api.app:app` from the repo root or as a script from within api/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn

from core.geo_middleware import GeoIPMiddleware
logger = logging.getLogger(__name__)

# ── API server lifecycle ──
API_HOST = "0.0.0.0"
API_PORT = 8001

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.status = "running"
    logger.info(f"[API] FastAPI started on {API_HOST}:{API_PORT}")
    yield
    # Shutdown
    logger.info("[API] FastAPI shutting down")

# Disable API docs in production
import os
IS_PRODUCTION = os.getenv("NODE_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"

app = FastAPI(
    title="ProToIt API",
    description="Property tool & AI-agent protocol for Chattanooga real estate",
    version="0.1.0",
    lifespan=lifespan,
    redirect_slashes=False,
    docs_url=None if IS_PRODUCTION else "/docs",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# ── Security helpers ──
import os
from urllib.parse import urlparse

def validate_origin(url: str) -> bool:
    """Strictly validate that an origin is a known HTTPS URL."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https':
            return False
        if not parsed.hostname:
            return False
        # Reject wildcard, empty, and malformed origins
        if '*' in url or not parsed.hostname:
            return False
        return True
    except Exception:
        return False

def get_allowed_origins() -> list[str]:
    raw = os.getenv("PROPTOIT_ALLOWED_ORIGINS", "https://proptoit.com")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    # Only allow valid HTTPS origins; reject wildcards
    validated = [o for o in origins if validate_origin(o)]
    if not validated:
        validated = ["https://proptoit.com"]  # safe default
    # Dev origins only outside production. Starlette matches origins
    # literally, so wildcard patterns like "http://localhost:*" never match —
    # enumerate explicit ports instead (more via PROPTOIT_ALLOWED_ORIGINS).
    if not IS_PRODUCTION:
        validated.extend([
            "http://localhost:3000", "http://localhost:5173",
            "http://localhost:8080", "http://127.0.0.1:3000",
            "http://127.0.0.1:5173", "http://127.0.0.1:8080",
        ])
    return validated

ALLOWED_ORIGINS = get_allowed_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization", "Content-Type", "X-Requested-With",
        "X-CSRF-Token", "Accept",
    ],
    allow_credentials=True,
)

# GeoIP (add via FastAPI — needs app.add_middleware)
app.add_middleware(GeoIPMiddleware)

CSP_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in CSP_HEADERS.items():
        response.headers[header] = value
    return response

# ── Mount routes (free core) ──
from core import health, properties, geocoded, fsbo, foreclosures, search, meilisearch_search, appraisal, market

app.include_router(health.router, prefix="/api/health")
app.include_router(properties.router, prefix="/api/properties")
app.include_router(properties.router, prefix="/properties")  # Alias for frontend compatibility
app.include_router(geocoded.router, prefix="/api/geocoded")
app.include_router(fsbo.router, prefix="/api/fsbo")
app.include_router(foreclosures.router, prefix="/api/foreclosures")
app.include_router(search.router, prefix="/api/search")
app.include_router(appraisal.router, prefix="/api/appraisal")
app.include_router(market.router, prefix="/api/market")
app.include_router(meilisearch_search.router, prefix="/api/meilisearch")
from core.opt_out import router as opt_out_router
app.include_router(opt_out_router, tags=["Opt-Out"])


@app.get("/health")
async def root():
    return {"status": app.state.status, "service": "proptoit-api"}


if __name__ == "__main__":
    uvicorn.run("api.app:app", host=API_HOST, port=API_PORT, reload=True)
