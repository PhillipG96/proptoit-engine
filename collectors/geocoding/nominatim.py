"""Nominatim (OpenStreetMap) single-address geocoder.

Wraps the per-address lookup the old scripts each re-implemented, adding the one
piece several of them lacked: a unified rate limiter plus backoff/retry on the
429/503/509 responses Nominatim returns under load (generalizing the ad-hoc 509
handling in geocode_sold_properties.py). An in-memory cache dedupes repeated
addresses (e.g. many condo units at one street address).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .config import (
    NOMINATIM_MAX_RETRIES,
    NOMINATIM_URL,
    REQUEST_INTERVAL,
    USER_AGENT,
)

log = logging.getLogger(__name__)

Coord = tuple[float, float]


class NominatimClient:
    """Rate-limited Nominatim client with retry/backoff and address dedup."""

    def __init__(
        self,
        rate: float = REQUEST_INTERVAL,
        user_agent: str = USER_AGENT,
        max_retries: int = NOMINATIM_MAX_RETRIES,
    ):
        self.rate = rate
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.max_retries = max_retries
        self._cache: dict[str, Optional[Coord]] = {}
        self._last_request = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.rate:
            time.sleep(self.rate - elapsed)
        self._last_request = time.monotonic()

    def _query(self, q: str) -> Optional[Coord]:
        import requests  # lazy

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = requests.get(
                    NOMINATIM_URL,
                    params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
                    headers=self.headers,
                    timeout=10,
                )
                if resp.status_code in (429, 503, 509):
                    wait = 60 * (attempt + 1)
                    log.warning("Nominatim %s for %r — backing off %ds", resp.status_code, q, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                hits = resp.json()
                if hits:
                    return float(hits[0]["lat"]), float(hits[0]["lon"])
                return None
            except requests.RequestException as exc:
                log.debug("Nominatim failed for %r: %s", q, exc)
                return None
        log.warning("Nominatim gave up after %d retries for %r", self.max_retries, q)
        return None

    def geocode_address(self, address: str, city: str = "", state: str = "") -> Optional[Coord]:
        """Geocode a single address, caching by the full query string."""
        q = ", ".join(p for p in (address, city, state, "USA") if p)
        if q in self._cache:
            return self._cache[q]
        result = self._query(q)
        self._cache[q] = result
        return result
