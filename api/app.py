"""
ProToIt API Layer — FastAPI Application Entry Point
=====================================================
Start: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, Request, HTTPException
import logging
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import sys
sys.path.insert(0, "/home/magicofelipe/proptoit/api")
from core.geo_middleware import GeoIPMiddleware
from auth_middleware import AuthMiddleware
from core.auth import router as auth_router
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
import re, os
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
    # Dev/mobile origins only outside production (hardcoded LAN IP removed)
    if not IS_PRODUCTION:
        validated.extend(["exp://*", "http://localhost:*", "http://127.0.0.1:*"])
    return validated

# ── Middleware ──

# CORS — validated origins only
ALLOWED_ORIGINS = get_allowed_origins()

# ── Auth middleware (runs before security headers) ──
app.add_middleware(AuthMiddleware)

# ── Auth routes ──
app.include_router(auth_router, prefix="/api/auth", tags=["Authentication"])

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

# ── Rate limit middleware (ASGI class — wraps app directly, not via add_middleware) ──
import json
from rate_limiter import SEARCH_LIMITER, API_KEY_LIMITER, DEFAULT_LIMITER
from fastapi.responses import JSONResponse


class RateLimitMiddleware:
    """ASGI rate-limit middleware — wraps the app directly (no BaseHTTPMiddleware wrapper)."""

    RATE_LIMIT_MAP = {
        "/api/search": SEARCH_LIMITER,
        "/api/appraisal": SEARCH_LIMITER,
        "/api/meilisearch": SEARCH_LIMITER,
        "/api/keys": API_KEY_LIMITER,
    }

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    @staticmethod
    def _resolve_client_ip(scope: dict) -> str:
        """Resolve the real client IP from trusted proxy headers.
        Only trusted: CF-Connecting-IP (Cloudflare), then last entry of X-Forwarded-For.
        Falls back to direct connection IP if behind a tunnel.
        """
        headers = dict(scope.get("headers", []))
        cf_ip = headers.get(b"cf-connecting-ip", b"")
        if cf_ip:
            return cf_ip.decode()
        xff = headers.get(b"x-forwarded-for", b"")
        if xff:
            # Last entry is the trusted proxy in a chain
            return xff.decode().rsplit(",", 1)[-1].strip()
        # Direct connection (e.g., behind tunnel without proxy header)
        client = scope.get("client")
        if client:
            return client[0]
        return "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = self._resolve_client_ip(scope)

        # Find matching limiter for this path
        limiter = DEFAULT_LIMITER
        for prefix, limiter_instance in self.RATE_LIMIT_MAP.items():
            if scope["path"].startswith(prefix):
                limiter = limiter_instance
                break

        # Rate limit check — reject if exceeded
        allowed, rate_info = limiter.is_allowed(client_ip)
        if not allowed:
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(rate_info.get("retry_after", 60)).encode()),
                    (b"x-ratelimit-limit", str(limiter.max_requests).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": json.dumps({"error": "Rate limit exceeded", **rate_info}).encode(),
            })
            return

        # Success — pass rate limiter info into the send wrapper
        rl_max = limiter.max_requests
        rl_remaining = rate_info.get("remaining", 0)
        rl_reset = rate_info.get("reset_at", 0)

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                existing = list(message.get("headers", []))
                existing.append((b"x-ratelimit-limit", str(rl_max).encode()))
                existing.append((b"x-ratelimit-remaining", str(rl_remaining).encode()))
                existing.append((b"x-ratelimit-reset", str(rl_reset).encode()))
                message["headers"] = existing
            await send(message)

        await self.app(scope, receive, wrapped_send)


# ── Middleware stack (outer to inner) ──
# 1. CORS (add via FastAPI)
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

# 2. GeoIP (add via FastAPI — needs app.add_middleware)
app.add_middleware(GeoIPMiddleware)

# 3. Body size limit (ASGI middleware — before rate limiting)
from middleware import BodySizeMiddleware

class BodySizeThenRateLimitMiddleware:
    """Check body size first, then pass to rate limiting."""
    def __init__(self, app):
        self.app = app
        self.middleware = BodySizeMiddleware(RateLimitMiddleware(app))

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        await self.middleware(scope, receive, send)

# ── Mount routes ──
from core import health, properties, geocoded, fsbo, foreclosures, search, meilisearch_search, appraisal, market, investor_router, admin_router
from core import deals as deals_router

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
app.include_router(deals_router.router, prefix="/api/deals", tags=["Deals"])
app.include_router(investor_router.router, prefix="/api/investors")
app.include_router(admin_router.router, prefix="/api/admin")
from core.opt_out import router as opt_out_router
app.include_router(opt_out_router, tags=["Opt-Out"])


# ── MCP HTTP endpoints (must be BEFORE wrapping) ──
MCP_TOOLS = [
    {
        "name": "search_properties",
        "description": "Search Chattanooga properties by address fragment, value band, and distress signals. Args: query (address fragment), distress_types (list of signals: pre_foreclosure, tax_delinquent, code_violation, probate, fsbo, vacancy), min_value, max_value, sort_by (distressScore|assessedValue|lastSalePrice|yearBuilt|buildingSqft|address), limit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Address fragment to match"},
                "distress_types": {"type": "array", "items": {"type": "string"}, "description": "Filter by distress signals"},
                "min_value": {"type": "number", "description": "Minimum assessed value"},
                "max_value": {"type": "number", "description": "Maximum assessed value"},
                "sort_by": {"type": "string", "default": "distressScore"},
                "limit": {"type": "integer", "default": 25}
            }
        }
    },
    {
        "name": "get_property_detail",
        "description": "Get full record for one parcel: address, assessment, owner, all distress signals. Args: parcel_id (Hamilton County parcel ID).",
        "inputSchema": {
            "type": "object",
            "properties": {"parcel_id": {"type": "string"}},
            "required": ["parcel_id"]
        }
    },
    {
        "name": "list_foreclosures",
        "description": "List Hamilton County foreclosure auctions joined with parcel addresses. Args: status (upcoming|active|completed), days_ahead, limit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "upcoming"},
                "days_ahead": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 50}
            }
        }
    },
    {
        "name": "get_distress_signals",
        "description": "Return all distress signals (with severity, confidence, source) for one parcel. Args: parcel_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"parcel_id": {"type": "string"}},
            "required": ["parcel_id"]
        }
    },
    {
        "name": "list_fsbo",
        "description": "List FSBO (For Sale By Owner) listings from Craigslist Chattanooga. Args: limit.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}}
        }
    }
]

MCP_TOOL_NAMES = [t["name"] for t in MCP_TOOLS]


def _get_api_key(request: Request) -> str | None:
    """Extract API key from Bearer token or x-api-key header."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("x-api-key")


from mcp_auth import validate_mcp_key

async def _require_mcp_key(request: Request):
    """Dependency that enforces API key auth and usage limits on MCP endpoints."""
    api_key = _get_api_key(request)
    if not api_key:
        raise HTTPException(status_code=401, detail="missing_api_key")

    user_info = validate_mcp_key(api_key)
    if not user_info:
        raise HTTPException(status_code=403, detail="invalid_or_revoked_key")

    user_id, key_tier, user_tier, email, count, max_calls = user_info
    if count >= max_calls:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly MCP call limit exceeded ({count}/{max_calls} calls). Upgrade at proptoit.io/pricing"
        )


@app.get("/api/mcp/tools")
async def mcp_tools_list(request: Request):
    """MCP tool discovery endpoint. Requires MCP-tier API key."""
    auth_info = request.scope.get("_auth_info")
    if not auth_info or auth_info.get("tier") != "mcp":
        raise HTTPException(status_code=403, detail={"error": "mcp_key_required", "message": "MCP-tier API key required for this endpoint"})
    return {
        "tools": MCP_TOOLS,
        "instructions": "PropToIt — Chattanooga off-market real estate intelligence. Read-only access to public-records property data: parcels, distress signals (pre-foreclosure, tax delinquent, code violation, probate, FSBO, vacancy), and Hamilton County foreclosure auctions."
    }


@app.post("/api/mcp/call/{tool_name}")
async def mcp_tool_call(tool_name: str, body: dict, request: Request):
    """Execute an MCP tool call via HTTP.

    POST /api/mcp/call/search_properties {"query": "Main St", "limit": 10}
    POST /api/mcp/call/get_property_detail {"parcel_id": "12345"}
    """
    auth_info = request.scope.get("_auth_info")
    if not auth_info or auth_info.get("tier") != "mcp":
        raise HTTPException(status_code=403, detail={"error": "mcp_key_required", "message": "MCP-tier API key required for this endpoint"})

    if tool_name not in MCP_TOOL_NAMES:
        return {"error": f"Tool '{tool_name}' not found. Available: {MCP_TOOL_NAMES}"}

    tool = next((t for t in MCP_TOOLS if t["name"] == tool_name), None)
    if tool is None:
        return {"error": f"Tool '{tool_name}' not found. Available: {MCP_TOOL_NAMES}"}

    # Validate body against the tool's inputSchema (type + unknown-key rejection)
    schema = tool.get("inputSchema", {})
    allowed = set(schema.get("properties", {}).keys())
    if not isinstance(body, dict):
        return {"error": "Request body must be a JSON object"}
    unknown = set(body.keys()) - allowed
    if unknown:
        return {"error": f"Unknown argument(s): {sorted(unknown)}. Allowed: {sorted(allowed)}"}
    for k, v in body.items():
        expected = schema["properties"][k].get("type", "string")
        pytype = {"string": str, "integer": int, "number": (int, float),
                  "boolean": bool, "array": list, "object": dict}.get(expected)
        if pytype is not None and not isinstance(v, pytype) or (pytype is int and isinstance(v, bool)):
            return {"error": f"Argument '{k}' must be of type {expected}"}

    # Import from mcp_server to reuse the actual implementations
    try:
        import mcp_server as ms
        fn = getattr(ms, tool_name, None)
        if fn is None:
            return {"error": f"Function '{tool_name}' not found in mcp_server"}
        result = fn(**body)
        return {"result": result}
    except Exception:
        logger.exception("MCP tool call failed: %s", tool_name)
        return {"error": "Internal error executing tool"}


@app.get("/health")
async def root():
    return {"status": app.state.status, "service": "proptoit-api"}


# ── Wrap entire stack with ASGI middleware after routes are registered ──
app = BodySizeThenRateLimitMiddleware(app)


if __name__ == "__main__":
    uvicorn.run("app:app", host=API_HOST, port=API_PORT, reload=True)
