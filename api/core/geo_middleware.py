"""
GeoIP middleware — blocks non-US traffic using MaxMind GeoLite2.
Free database from https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
"""
import maxminddb
import logging
import os
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
logger = logging.getLogger(__name__)

# Allowed country codes — expandable
ALLOWED_COUNTRIES = {"US"}

# Database path — REQUIRED via GEOLITE_DB env var (no developer-home default).
# Get a free key and download via: python -m api.core.geo_download
DB_PATH = Path(os.environ["GEOLITE_DB"]) if os.getenv("GEOLITE_DB") else None

_geo_db = None


def _get_db():
    global _geo_db
    if _geo_db is None:
        if DB_PATH is None or not DB_PATH.exists():
            logger.info(f"[geo] Warning: GeoLite2 DB not available (set GEOLITE_DB; got {DB_PATH})")
            logger.info("[geo] Run: python -m api.core.geo_download")
            return None
        try:
            _geo_db = maxminddb.open_database(str(DB_PATH))
        except Exception as e:
            logger.info(f"[geo] Failed to load GeoLite2: {e}")
            return None
    return _geo_db


def get_country(client_ip: str) -> str | None:
    """Return 2-letter country code for an IP, or None."""
    db = _get_db()
    if db is None:
        return None
    try:
        record = db.get(client_ip)
        if record and isinstance(record, dict):
            country = record.get("country", {})
            if isinstance(country, dict):
                code = country.get("iso_code")
                return str(code) if code else None
    except Exception:
        pass
    return None


class GeoIPMiddleware(BaseHTTPMiddleware):
    """Block non-US IPs. Skips for health checks and known proxies."""

    BYPASS_PATHS = ["/health", "/api/health", "/favicon"]

    def _client_ip(self, request: Request) -> str:
        """Extract real client IP from proxy headers.

        Cloudflare/forwarded headers are attacker-controlled unless the
        request actually arrived through a trusted proxy. This box is fronted
        by a Cloudflare Tunnel (origin = localhost), so we trust the headers
        only when the socket peer is loopback/private; otherwise use the
        socket peer directly.
        """
        peer = request.client.host if request.client else "0.0.0.0"
        try:
            import ipaddress
            trusted = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
        except ValueError:
            trusted = False
        if trusted:
            cf = request.headers.get("cf-connecting-ip")
            if cf:
                return cf
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
        return peer

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip health checks
        if any(request.url.path.startswith(p) for p in self.BYPASS_PATHS):
            return await call_next(request)

        client_ip = self._client_ip(request)
        country = get_country(client_ip)

        # If we can't determine country, allow through
        if country is None:
            return await call_next(request)

        # Block non-allowed countries
        if country not in ALLOWED_COUNTRIES:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Access denied",
                    "message": "Service only available in the United States"
                },
                headers={"X-Country-Blocked": country}
            )

        return await call_next(request)
