"""US Census batch geocoder.

The HTTP call lives in :func:`census_batch`; the request body builder and the
response parser are kept pure (and ``requests`` is imported lazily) so they can
be unit-tested without the network or the dependency.
"""
from __future__ import annotations

import csv
import io
import logging

from .addresses import normalize_address
from .config import CENSUS_BENCHMARK, CENSUS_URL

log = logging.getLogger(__name__)


def build_census_csv(rows: list[dict]) -> str:
    """Build the headerless ``id,street,city,state,zip`` CSV the Census API wants."""
    buf = io.StringIO()
    for r in rows:
        addr = normalize_address(r["address"]).replace('"', "'")
        city = r.get("city") or ""
        state = r.get("state") or ""
        zip_ = r.get("postal_code") or ""
        buf.write(f'{r["id"]},"{addr}","{city}","{state}","{zip_}"\n')
    return buf.getvalue()


def parse_census_response(text: str) -> dict[int, tuple[float, float]]:
    """Parse Census batch CSV output into ``{id: (lat, lng)}``.

    Only ``Match`` rows are kept. Census returns the coordinate as ``lng,lat``
    (x,y) in column 5 — we swap it to ``(lat, lng)``.
    """
    results: dict[int, tuple[float, float]] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 6:
            continue
        if row[2].strip().lower() != "match":
            continue
        try:
            db_id = int(row[0].strip())
            lng, lat = row[5].strip().split(",")
            results[db_id] = (float(lat), float(lng))
        except (ValueError, IndexError):
            continue
    return results


def census_batch(rows: list[dict], timeout: int = 180) -> dict[int, tuple[float, float]]:
    """Geocode a batch of rows via the Census API. Returns ``{id: (lat, lng)}``."""
    if not rows:
        return {}
    import requests  # lazy: keeps the pure helpers import-light

    payload = build_census_csv(rows).encode()
    log.info("Census batch: %d addresses", len(rows))
    try:
        resp = requests.post(
            CENSUS_URL,
            files={
                "addressFile": ("addresses.csv", payload, "text/csv"),
                "benchmark": (None, CENSUS_BENCHMARK),
                "returntype": (None, "locations"),
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Census API request failed: %s", exc)
        return {}

    results = parse_census_response(resp.text)
    log.info("Census matched %d / %d", len(results), len(rows))
    return results
