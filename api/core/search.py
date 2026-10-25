"""
Search endpoint — Meilisearch full-text + PostgreSQL filter routing.
Supports free-text query (q) via Meilisearch, and filter-based search via PostgreSQL.
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any
import http.client
import json
import os

from core.queries import search_properties
from sanitize import sanitize_query_param, sanitize_list_param, sanitize_number, sanitize_url_path
from cache import cache
import re

router = APIRouter()

MEILI_HOST = os.getenv("MEILI_HOST", "localhost")
MEILI_PORT = int(os.getenv("MEILI_PORT", "7707"))
MEILI_INDEX = os.getenv("MEILI_INDEX", "properties")
def _meili_key() -> str:
    """Resolve the Meilisearch key at call time so importing this module
    doesn't require env vars (the key is env-only, never hardcoded)."""
    key = os.getenv("MEILI_MASTER_KEY")
    if not key:
        raise RuntimeError(
            "MEILI_MASTER_KEY environment variable is required. "
            "Set MEILI_MASTER_KEY to your Meilisearch master API key."
        )
    return key


def _meilisearch_search(q, limit, offset, county_id=None):
    """Search via Meilisearch with an optional county filter.

    Note: distress_types filtering only applies on the PostgreSQL
    (filter-based) path — the Meilisearch index carries no per-signal
    filterable fields, so free-text search cannot honor it.
    """
    body = {"q": q, "limit": limit, "offset": offset, "showRankingScore": True}
    if county_id:
        # county_id is an alphabetic slug (e.g. "hamilton"); quote it for Meili filter syntax
        safe = "".join(ch for ch in county_id if ch.isalnum() or ch == "_")
        body["filter"] = f'county_id = "{safe}"'
    conn = http.client.HTTPConnection(MEILI_HOST, MEILI_PORT, timeout=15)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_meili_key()}",
    }
    try:
        conn.request("POST", f"/indexes/{MEILI_INDEX}/search", json.dumps(body), headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise HTTPException(502, f"Meilisearch search failed (HTTP {resp.status})")
        data = json.loads(raw.decode())
    finally:
        conn.close()
    hits = data.get("hits", [])
    # Strip ranking score from output but include total
    clean_hits = []
    for h in hits:
        item = {k: v for k, v in h.items() if k != "_rankingScore"}
        clean_hits.append(item)
    return {
        "data": clean_hits,
        "total": data.get("estimatedTotalHits", len(hits)),
        "limit": limit,
        "offset": offset,
        "query": q,
    }


@router.get("")
@router.get("/")
def search_properties_endpoint(
    q: Optional[str] = Query(default=None, description="Free-text query (address, parcel, owner)"),
    limit: int = Query(default=25, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    distress_types: Optional[List[str]] = Query(default=None),
    min_assessed_value: Optional[float] = Query(default=None, alias="minAssessedValue"),
    max_assessed_value: Optional[float] = Query(default=None, alias="maxAssessedValue"),
    address: Optional[str] = Query(default=None),
    parcel_id: Optional[str] = Query(default=None, alias="parcelId"),
    has_distress_signals: Optional[bool] = Query(default=None, alias="hasDistressSignals"),
    signal_type: Optional[str] = Query(default=None, alias="signalType"),
    county_id: Optional[str] = Query(default=None, description="County slug (e.g. hamilton, harris). None = all counties."),
) -> Dict[str, Any]:
    """Search properties: free-text via Meilisearch, or filter-based via PostgreSQL.

    distress_types (and signal_type) only apply to the filter-based PostgreSQL
    path; free-text search cannot honor them.
    """
    # ── Input sanitization ─────────────────────────────────────────
    # Sanitize free-text query — strip HTML, truncate, remove null bytes
    q_clean = sanitize_query_param(q, max_length=255) if q else ""
    # Sanitize address — strip HTML, truncate
    address_clean = sanitize_query_param(address, max_length=255) if address else None
    # Sanitize parcel_id — only safe path characters
    parcel_id_clean = sanitize_url_path(parcel_id) if parcel_id else None
    # Sanitize distress types — validate each item, limit count
    distress_clean = sanitize_list_param(distress_types, max_items=25) if distress_types else None
    # Sanitize numeric parameters with safe defaults
    limit = int(sanitize_number(limit, min_val=1, max_val=1000, default=25))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    min_val = sanitize_number(min_assessed_value, min_val=0, max_val=100_000_000, default=None)
    max_val = sanitize_number(max_assessed_value, min_val=0, max_val=100_000_000, default=None)
    # Validate sort_by / sort_dir — only allow known fields
    valid_sort_fields = {
        "distressScore", "assessedValue", "lastSalePrice",
        "yearBuilt", "buildingSqft", "address", "county_id",
    }
    if sort_by not in valid_sort_fields:
        sort_by = "distressScore"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    # Validate county_id — invalid slugs fall back to the default; None/empty
    # searches all counties (meili path sends no filter; the SQL path skips
    # the county condition).
    if county_id and not re.match(r'^[a-z0-9_]+$', county_id):
        county_id = "hamilton"

    # Normalize signal_type
    if signal_type and not distress_clean:
        signal_clean = sanitize_query_param(signal_type, max_length=50)
        if signal_clean:
            distress_clean = [signal_clean]

    # Free-text query → Meilisearch
    if q_clean:
        return _meilisearch_search(q_clean, limit, offset, county_id)

    # Filter-only → PostgreSQL
    return search_properties(
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
        distress_types=distress_clean,
        min_assessed_value=min_val,
        max_assessed_value=max_val,
        address=address_clean,
        parcel_id=parcel_id_clean,
        county_id=county_id,
        cache=cache,
    )