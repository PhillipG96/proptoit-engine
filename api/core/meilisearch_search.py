"""
import logging

logger = logging.getLogger(__name__)

Meilisearch-powered full-text search endpoint.
"""
from fastapi import APIRouter, Query
from typing import Optional, Dict, Any, List
import http.client
import json
import os
from sanitize import sanitize_query_param, sanitize_number

router = APIRouter()

MEILI_HOST = os.getenv("MEILI_HOST", "localhost")
MEILI_PORT = int(os.getenv("MEILI_PORT", "7700"))
MEILI_INDEX = os.getenv("MEILI_INDEX", "properties")
MEILI_KEY = os.getenv("MEILI_MASTER_KEY")
if not MEILI_KEY:
    raise ValueError(
        "MEILI_MASTER_KEY environment variable is required. "
        "Set MEILI_MASTER_KEY to your Meilisearch master API key."
    )


@router.get("")
@router.get("/")
def meilisearch_search(
    q: str = Query(default="", description="Full-text query (address, parcel, owner)"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Full-text search via Meilisearch."""
    # ── Input sanitization ─────────────────────────────────────────
    q = sanitize_query_param(q, max_length=255)
    limit = int(sanitize_number(limit, min_val=1, max_val=100, default=25))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    if not q:
        return {"results": [], "total": 0, "limit": limit, "offset": offset}

    conn = http.client.HTTPConnection(MEILI_HOST, MEILI_PORT, timeout=10)
    body = json.dumps({"q": q, "limit": limit, "offset": offset})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MEILI_KEY}",
    }
    conn.request("POST", f"/indexes/{MEILI_INDEX}/search", body, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()

    hits = data.get("hits", [])
    return {
        "results": hits,
        "total": data.get("estimatedTotalHits", len(hits)),
        "limit": limit,
        "offset": offset,
        "query": q,
    }
