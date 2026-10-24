"""
import logging

logger = logging.getLogger(__name__)

FSBO Listings API — exposes Craigslist FSBO data with property matches.
"""
from typing import Optional
from fastapi import APIRouter, Query
from core import queries
from sanitize import sanitize_number, validate_county_id

router = APIRouter()


@router.get("")
async def list_fsbo(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    county_id: Optional[str] = Query(default="hamilton"),
) -> dict:
    """List FSBO listings from Craigslist with optional property matches."""
    # ── Input sanitization ─────────────────────────────────────────
    limit = int(sanitize_number(limit, min_val=1, max_val=500, default=100))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    county_id = validate_county_id(county_id)
    return queries.list_fsbo_listings(limit=limit, offset=offset, county_id=county_id)
