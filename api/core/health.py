"""
import logging

logger = logging.getLogger(__name__)

Health check endpoint for FastAPI server — returns dashboard stats
"""
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Query
from . import queries

router = APIRouter()


@router.get("")
@router.get("/")
async def health_check(county_id: Optional[str] = Query(default="hamilton")) -> Dict[str, Any]:
    """Full health + stats for dashboard"""
    total_props = queries.count_properties(county_id=county_id)
    distressed = queries.count_distressed(county_id=county_id)
    fsbo = queries.count_fsbo(county_id=county_id)
    pre_fcl = queries.count_pre_foreclosure(county_id=county_id)
    tax_del = queries.count_tax_delinquent(county_id=county_id)
    last_scraped = queries.last_scraped_at(county_id=county_id)
    categories = queries.count_properties_by_category(county_id=county_id)

    return {
        "status": "ok",
        "totalProperties": total_props,
        "distressedCount": distressed,
        "fsboCount": fsbo,
        "preForeclosureCount": pre_fcl,
        "taxDelinquentCount": tax_del,
        "lastScrapedAt": last_scraped,
        "propertyCategories": {(c.get("category") or c.get("property_category")): c["count"] for c in categories},
    }


@router.get("/status")
async def server_status():
    """Detailed status with uptime info"""
    return {
        "service": "ProToIt API",
        "version": "0.1.0",
        "status": "running",
        "uptime_check": True,
    }
