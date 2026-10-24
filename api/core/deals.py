"""
Curated Deal Feed — verified distressed properties, ranked.

Backed entirely by verified signals (tax_delinquent via the county
CTRUDELQ file, probate via published notices). Every property returned
carries source attribution and provenance dates. No heuristic signals
exist in the pipeline, so everything shown is real.

Endpoint: GET /api/deals
"""
import logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, Query
import http.client
import json
import os

from sanitize import sanitize_query_param, sanitize_number

logger = logging.getLogger(__name__)

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

SIGNAL_LABELS = {
    "tax_delinquent": "Tax delinquent (Hamilton County Trustee file)",
    "probate": "Probate estate (published notice)",
    "pre_foreclosure": "Foreclosure sale notice",
}

SOURCE_ATTRIBUTION = {
    "tax_delinquent": {
        "source": "Hamilton County Trustee delinquent file (CTRUDELQ)",
        "url": "https://www.hamiltontn.gov/_downloadsTrusteeDelinquent/CTRUDELQ.zip",
        "refresh": "Weekly (Wednesdays)",
    },
    "probate": {
        "source": "Chattanooga Times Free Press legal notices",
        "url": "https://www.timesfreepress.com/classifieds/category/legal-notices/",
        "refresh": "Weekly (Mondays)",
    },
    "pre_foreclosure": {
        "source": "Trustee's foreclosure sale notice (TFP legal notices)",
        "url": "https://www.timesfreepress.com/classifieds/category/legal-notices/",
        "refresh": "Weekly (Mondays)",
    },
}


def _meili_search(body: dict) -> dict:
    conn = http.client.HTTPConnection(MEILI_HOST, MEILI_PORT, timeout=10)
    conn.request("POST", f"/indexes/{MEILI_INDEX}/search", json.dumps(body),
                 headers={"Content-Type": "application/json",
                          "Authorization": f"Bearer {MEILI_KEY}"})
    data = json.loads(conn.getresponse().read().decode())
    conn.close()
    return data


def _signal_types(h: dict) -> list:
    types = []
    if h.get("is_tax_delinquent"):
        types.append("tax_delinquent")
    if h.get("probate_flag"):
        types.append("probate")
    if h.get("pre_foreclosure_flag"):
        types.append("pre_foreclosure")
    if h.get("fsbo_flag"):
        types.append("fsbo")
    return types


def _format_deal(h: dict) -> dict:
    """Shape a meili hit into a deal-feed entry with provenance."""
    equity = None
    if h.get("assessed_value") and h.get("last_sale_price"):
        equity = round(h["assessed_value"] - h["last_sale_price"])
    types = _signal_types(h)
    return {
        "propertyId": h.get("id"),
        "parcelId": h.get("parcel_id"),
        "address": h.get("address"),
        "city": h.get("city"),
        "state": h.get("state") or "TN",
        "ownerOfRecord": h.get("owner_of_record"),
        "ownerOccupant": h.get("owner_occupant", False),
        "dealScore": h.get("deal_score", 0),
        "distressScore": h.get("distress_score", 0),
        "signals": [
            {"type": t, "label": SIGNAL_LABELS.get(t, t),
             "attribution": SOURCE_ATTRIBUTION.get(t)} for t in types
        ],
        "assessedValue": h.get("assessed_value"),
        "lastSalePrice": h.get("last_sale_price"),
        "lastSaleDate": h.get("last_sale_date"),
        "estimatedEquity": equity,
        "yearBuilt": h.get("year_built"),
        "buildingSqft": h.get("building_sqft"),
        "lat": h.get("_geo", {}).get("lat") if h.get("_geo") else h.get("lat"),
        "lng": h.get("_geo", {}).get("lng") if h.get("_geo") else h.get("lng"),
    }


@router.get("")
@router.get("/")
def deals_feed(
    signal: Optional[str] = Query(default=None,
        description="Filter: tax_delinquent | probate | pre_foreclosure"),
    min_deal_score: int = Query(default=0, ge=0, le=100),
    min_equity: Optional[int] = Query(default=None, ge=0,
        description="Minimum estimated equity ($) — assessed_value minus last_sale_price"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Curated feed of verified distressed properties, ranked by deal score."""
    signal = sanitize_query_param(signal, max_length=40) if signal else None
    limit = int(sanitize_number(limit, min_val=1, max_val=100, default=25) or 25)
    offset = int(sanitize_number(offset, min_val=0, default=0) or 0)
    min_deal_score = int(sanitize_number(min_deal_score, min_val=0, max_val=100, default=0) or 0)

    filters = ["(is_tax_delinquent = true OR probate_flag = true OR pre_foreclosure_flag = true)"]
    if min_deal_score > 0:
        filters.append(f"deal_score >= {min_deal_score}")
    if min_equity is not None:
        filters.append(f"estimated_equity >= {int(min_equity)}")
    if signal == "tax_delinquent":
        filters = ["is_tax_delinquent = true"]
        if min_deal_score > 0:
            filters.append(f"deal_score >= {min_deal_score}")
        if min_equity is not None:
            filters.append(f"estimated_equity >= {int(min_equity)}")
    elif signal == "probate":
        filters = ["probate_flag = true"]
    elif signal == "pre_foreclosure":
        filters = ["pre_foreclosure_flag = true"]

    body = {
        "q": "",
        "filter": " AND ".join(filters),
        "sort": ["deal_score:desc"],
        "limit": limit,
        "offset": offset,
    }
    data = _meili_search(body)
    hits = data.get("hits", [])

    return {
        "deals": [_format_deal(h) for h in hits],
        "total": data.get("estimatedTotalHits", len(hits)),
        "limit": limit,
        "offset": offset,
        "filters": {
            "signal": signal,
            "minDealScore": min_deal_score,
            "minEquity": min_equity,
        },
        "attribution": {
            "tax_delinquent": SOURCE_ATTRIBUTION["tax_delinquent"],
            "probate": SOURCE_ATTRIBUTION["probate"],
            "pre_foreclosure": SOURCE_ATTRIBUTION["pre_foreclosure"],
        },
    }


@router.get("/stats")
def deals_stats() -> Dict[str, Any]:
    """Aggregate counts for the deal feed header."""
    data = _meili_search({"q": "", "facets": ["is_tax_delinquent", "probate_flag",
                                              "pre_foreclosure_flag"], "limit": 0})
    facets = data.get("facetDistribution", {})
    return {
        "taxDelinquent": facets.get("is_tax_delinquent", {}).get("true", 0),
        "probate": facets.get("probate_flag", {}).get("true", 0),
        "preForeclosure": facets.get("pre_foreclosure_flag", {}).get("true", 0),
        "totalWithSignals": sum(v.get("true", 0) for v in facets.values()),
        "attribution": SOURCE_ATTRIBUTION,
    }
