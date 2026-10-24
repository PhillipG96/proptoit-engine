"""
Foreclosure endpoints — properties flagged as pre-foreclosure + auction data.
"""
import logging
import re
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, Query

from . import queries
from sanitize import sanitize_query_param, sanitize_number, validate_county_id

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("")
def get_foreclosures(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="distressScore"),
    sort_dir: str = Query(default="desc"),
    address: Optional[str] = Query(default=None),
    county_id: Optional[str] = Query(default="hamilton"),
) -> Dict[str, Any]:
    """Return properties with pre_foreclosure distress signal + auction timeline info."""
    # ── Input sanitization ─────────────────────────────────────────
    valid_sort_fields = {"distressScore", "assessedValue", "lastSalePrice", "address"}
    if sort_by not in valid_sort_fields:
        sort_by = "distressScore"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    limit = int(sanitize_number(limit, min_val=1, max_val=200, default=50))
    offset = int(sanitize_number(offset, min_val=0, default=0))
    address_clean = sanitize_query_param(address, max_length=255) if address else None
    county_id = validate_county_id(county_id)
    try:
        props = queries.search_properties(
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
            distress_types=["pre_foreclosure"],
            address=address_clean,
            county_id=county_id,
        )
        # Fetch auction timeline for returned properties (at_risk = pre-foreclosure/upcoming)
        foreclosure_rows = queries.list_foreclosures(
            status="at_risk",
            days_ahead=365,
            limit=limit * 2,
        )
        auction_map: dict[str, dict] = {}
        for f in foreclosure_rows:
            auction_map[f["parcelId"]] = f

        # Attach timeline to each property
        for prop in props["data"]:
            pid = prop["parcelId"]
            prop["foreclosureTimeline"] = auction_map.get(pid)

        return {
            "success": True,
            "data": props["data"],
            "pagination": {
                "total": props["total"],
                "limit": props["limit"],
                "offset": props["offset"],
            },
        }
    except Exception:
        logger.error("Foreclosures query error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats")
async def get_foreclosure_stats(county_id: Optional[str] = Query(default="hamilton")) -> Dict[str, Any]:
    """Return foreclosure-specific summary statistics."""
    # ── Input sanitization ─────────────────────────────────────────
    county_id = validate_county_id(county_id)
    try:
        total_pre = queries.count_pre_foreclosure(county_id=county_id)
        active_auctions = queries.count_foreclosures(status="active", days_ahead=30, county_id=county_id)
        upcoming_auctions = queries.count_foreclosures(status="at_risk", days_ahead=365, county_id=county_id)

        return {
            "success": True,
            "totalPreForeclosures": total_pre,
            "activeAuctions": active_auctions,
            "upcomingAuctions": upcoming_auctions,
        }
    except Exception:
        logger.error("Foreclosure stats query error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
