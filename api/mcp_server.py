"""
import logging

logger = logging.getLogger(__name__)

PropToIt MCP server — exposes read-only property intelligence tools over stdio.

Run:
    python mcp_server.py

MCP client config (e.g. Claude Desktop / Claude Code):
    {
        "mcpServers": {
            "proptoit": {
                "command": "python",
                "args": ["/home/magicofelipe/proptoit/api/mcp_server.py"]
            }
        }
    }
"""
import json
import sys
from pathlib import Path
from typing import Optional, Any

from dotenv import load_dotenv

# core.meilisearch_search needs the Meilisearch auth key at import time; when run
# under an MCP client that passes a minimal env, pull it from the project .env.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write(
        "Missing dependency: mcp. Install with `pip install 'mcp>=1.4.0'`.\n"
    )
    raise

from core import queries
from core import appraisal
from core import market as market_scoring
from core import meilisearch_search as ms
from knowledge_layer import ContextAssembler, RetrievalContract

mcp = FastMCP(
    "proptoit",
    instructions=(
        "PropToIt — Chattanooga off-market real estate intelligence. "
        "Read-only access to public-records property data: parcels, distress "
        "signals (pre-foreclosure, tax delinquent, code violation, probate, "
        "FSBO, vacancy), and Hamilton County foreclosure auctions."
    ),
)


@mcp.tool()
def search_properties(
    query: Optional[str] = "",
    distress_types: Optional[list[str]] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    sort_by: str = "distressScore",
    limit: int = 25,
    county_id: str = "hamilton",
) -> str:
    """Search Chattanooga properties by address fragment, value band, and distress signals.

    Args:
        query: Address fragment to match (case-insensitive partial).
        distress_types: Filter to parcels with at least one of these signals.
            Accepts canonical names (pre_foreclosure, tax_delinquent, code_violation,
            probate, fsbo, vacancy) or camelCase flag names (preForeclosureFlag, etc.).
        min_value: Minimum assessed value (USD).
        max_value: Maximum assessed value (USD).
        sort_by: One of distressScore, assessedValue, lastSalePrice, yearBuilt,
            buildingSqft, address. Default distressScore (highest first).
        limit: Max rows to return (default 25, hard cap 200).
        county_id: County identifier (default 'hamilton').
    """
    capped = max(1, min(int(limit), 200))
    result = queries.search_properties(
        limit=capped,
        offset=0,
        sort_by=sort_by,
        sort_dir="desc",
        distress_types=distress_types,
        min_assessed_value=min_value,
        max_assessed_value=max_value,
        address=query,
        county_id=county_id,
    )
    summaries = [
        {
            "parcelId":      p["parcelId"],
            "address":       p["address"],
            "assessedValue": p["assessedValue"],
            "distressScore": p["distressScore"],
            "signals": [
                k.replace("Flag", "").replace("Heuristic", "")
                for k in (
                    "preForeclosureFlag", "taxDelinquentFlag", "codeViolationFlag",
                    "probateFlag", "fsboFlag", "vacancyHeuristic",
                )
                if p.get(k)
            ],
        }
        for p in result["data"]
    ]
    return json.dumps({"count": len(summaries), "total": result["total"], "results": summaries}, indent=2)


@mcp.tool()
def get_property_detail(
    parcel_id: str,
    county_id: str = "hamilton",
) -> str:
    """Get full record for one parcel: address, assessment, owner, all distress signals.

    Args:
        parcel_id: Hamilton County parcel ID (exact match).
        county_id: County identifier (default 'hamilton').
    """
    prop = queries.get_property_by_parcel_id(parcel_id, county_id=county_id)
    if not prop:
        return json.dumps({"error": "not_found", "parcelId": parcel_id})

    signals = queries.get_distress_signals_for_parcel(parcel_id, county_id=county_id)
    foreclosures = [
        f for f in queries.list_foreclosures(status="upcoming", days_ahead=365, limit=10)
        if f["parcelId"] == parcel_id
    ]
    return json.dumps(
        {"property": prop, "distressSignals": signals, "foreclosures": foreclosures},
        indent=2,
        default=str,
    )


@mcp.tool()
def list_foreclosures(status: str = "upcoming", days_ahead: int = 30, limit: int = 50) -> str:
    """List Hamilton County foreclosure auctions joined with parcel addresses.

    Args:
        status: One of 'upcoming', 'active', 'completed'. Default 'upcoming'.
        days_ahead: Look ahead this many days from today (default 30).
        limit: Max rows (default 50, cap 200).
    """
    capped_limit = max(1, min(int(limit), 200))
    capped_days = max(0, min(int(days_ahead), 365))
    rows = queries.list_foreclosures(status=status, days_ahead=capped_days, limit=capped_limit)
    return json.dumps({"count": len(rows), "foreclosures": rows}, indent=2, default=str)


@mcp.tool()
def get_distress_signals(
    parcel_id: str,
    county_id: str = "hamilton",
) -> str:
    """Return all distress signals (with severity, confidence, source) for one parcel.

    Args:
        parcel_id: County parcel ID (exact match).
        county_id: County identifier (default 'hamilton').
    """
    signals = queries.get_distress_signals_for_parcel(parcel_id, county_id=county_id)
    return json.dumps({"parcelId": parcel_id, "count": len(signals), "signals": signals}, indent=2, default=str)


@mcp.tool()
def list_fsbo(limit: int = 50, county_id: str = "hamilton") -> str:
    """List FSBO (For Sale By Owner) listings from Craigslist Chattanooga.

    Args:
        limit: Max listings to return (default 50, cap 200).
        county_id: County identifier (default 'hamilton').
    """
    capped = max(1, min(int(limit), 200))
    result = queries.list_fsbo_listings(limit=capped, offset=0, county_id=county_id)
    return json.dumps(result, indent=2, default=str)


# Global assembler instance
_assembler = ContextAssembler()


@mcp.tool()
def assemble_context(
    task_type: str = "deal_analysis",
    parcel_id: str = "",
    required_sources: Optional[list[str]] = None,
    include_prior_memory: bool = True,
    freshness_hours: int = 24,
) -> str:
    """Assemble a prepared context bundle for an agent task.

    Instead of calling multiple tools separately, this returns a single
    structured bundle with all the data the agent needs for a specific task.
    Bundles are cached so repeated runs don't re-fetch the same data.

    Args:
        task_type: The work primitive (deal_analysis, fsbo_outreach,
                   foreclosure_monitor, property_search).
        parcel_id: Hamilton County parcel ID (exact match).
        required_sources: Sources to include (assessment, distress_signals,
                          fsbo, foreclosures, agent_memory). Default: all.
        include_prior_memory: Include prior agent observations. Default True.
        freshness_hours: Cache TTL in hours. Default 24.
    """
    sources = required_sources or [
        "assessment", "distress_signals", "fsbo", "foreclosures", "agent_memory"
    ]

    contract = RetrievalContract(
        task_type=task_type,
        work_object=parcel_id,
        required_sources=sources,
        include_prior_memory=include_prior_memory,
        freshness_threshold_hours=freshness_hours,
    )

    bundle = _assembler.assemble(contract)
    return json.dumps({
        "bundleId": bundle.bundle_id[:16],
        "taskType": task_type,
        "workObject": bundle.work_object,
        "compiledAt": bundle.compiled_at,
        "sources": [s.model_dump() for s in bundle.sources],
        "data": bundle.data,
    }, indent=2, default=str)


@mcp.tool()
def save_agent_memory(
    parcel_id: str,
    content: str,
    confidence: str = "inferred",
) -> str:
    """Save an agent observation to persistent memory.

    Observations are labeled with confidence levels so future runs can
    distinguish confirmed facts from model guesses.

    Args:
        parcel_id: Hamilton County parcel ID (exact match).
        content: The observation or finding to store.
        confidence: Confidence label. One of: observed, inferred,
                    user_confirmed, stale, rejected, authoritative.
                    Default: inferred.
    """
    from knowledge_layer.memory import AgentMemory
    row_id = AgentMemory.save(parcel_id, content, confidence)
    return json.dumps({
        "memoryId": row_id,
        "parcelId": parcel_id,
        "confidence": confidence,
        "status": "saved",
    }, indent=2)


@mcp.tool()
def compare_properties(
    parcel_ids: list[str],
    county_id: str = "hamilton",
) -> str:
    """Side-by-side comparison of 2-10 properties by parcel ID.

    Args:
        parcel_ids: County parcel IDs to compare (2-10).
        county_id: County identifier (default 'hamilton').
    """
    if not parcel_ids or len(parcel_ids) < 2:
        return json.dumps({"error": "provide at least 2 parcel IDs"})
    if len(parcel_ids) > 10:
        return json.dumps({"error": "max 10 parcel IDs allowed"})

    props = queries.compare_properties(parcel_ids, county_id=county_id)
    if len(props) != len(parcel_ids):
        missing = set(parcel_ids) - {p["parcelId"] for p in props}
        return json.dumps({"found": len(props), "missing": list(missing), "properties": props}, indent=2, default=str)

    return json.dumps({"count": len(props), "properties": props}, indent=2, default=str)


@mcp.tool()
def get_top_deals(
    limit: int = 25,
    min_score: int = 50,
    county_id: str = "hamilton",
) -> str:
    """Get highest deal_score properties — the hottest off-market opportunities.

    Args:
        limit: Max properties to return (default 25, cap 200).
        min_score: Minimum deal_score filter (0-100, default 50).
        county_id: County identifier (default 'hamilton').
    """
    capped = max(1, min(int(limit), 200))
    capped_score = max(0, min(int(min_score), 100))
    result = queries.get_top_deals(limit=capped, min_score=capped_score, county_id=county_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_property_analysis(
    parcel_id: str,
    county_id: str = "hamilton",
) -> str:
    """Get deal analysis breakdown for a single property: equity, distress, scoring components.

    Args:
        parcel_id: County parcel ID (exact match).
        county_id: County identifier (default 'hamilton').
    """
    analysis = queries.get_property_analysis(parcel_id, county_id=county_id)
    if not analysis:
        return json.dumps({"error": "not_found", "parcelId": parcel_id})
    return json.dumps(analysis, indent=2, default=str)


@mcp.tool()
def list_distressed_with_coords(
    signal_type: Optional[str] = None,
    limit: int = 100,
    county_id: str = "hamilton",
) -> str:
    """List distressed properties with coordinates for map rendering.

    Only returns properties with valid lat/lng. Each row is one property-signal pair,
    so properties with multiple signals appear multiple times.

    Args:
        signal_type: Filter by signal type (pre_foreclosure, tax_delinquent,
            code_violation, probate, fsbo, vacancy). Default: all signals.
        limit: Max rows (default 100, cap 500).
        county_id: County identifier (default 'hamilton').
    """
    capped = max(1, min(int(limit), 500))
    rows = queries.list_distressed(signal_type=signal_type, limit=capped, county_id=county_id)
    return json.dumps({"count": len(rows), "data": rows}, indent=2, default=str)


@mcp.tool()
def get_system_stats() -> str:
    """Get system-wide statistics: total properties, distressed count, category breakdown,
    foreclosure counts, data freshness.

    Useful for monitoring dashboards and initial system overview.
    """
    return json.dumps({
        "total_properties": queries.count_properties(),
        "distressed_properties": queries.count_distressed(),
        "pre_foreclosure_count": queries.count_pre_foreclosure(),
        "tax_delinquent_count": queries.count_tax_delinquent(),
        "fsbo_listings": queries.count_fsbo(),
        "upcoming_auctions": queries.count_foreclosures(status="upcoming", days_ahead=30),
        "at_risk_auctions": queries.count_foreclosures(status="at_risk", days_ahead=30),
        "category_breakdown": queries.count_properties_by_category(),
        "last_data_update": queries.last_scraped_at(),
    }, indent=2, default=str)


@mcp.tool()
def search_by_category(
    category: str,
    query: Optional[str] = None,
    distress_types: Optional[list[str]] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    sort_by: str = "distressScore",
    limit: int = 25,
    county_id: str = "hamilton",
) -> str:
    """Search properties within a specific category (residential, commercial, exempt, agricultural).

    Args:
        category: Property category. One of: residential, commercial, exempt, agricultural.
        query: Address fragment to match (case-insensitive partial).
        distress_types: Filter to parcels with matching signals.
        min_value: Minimum assessed value (USD).
        max_value: Maximum assessed value (USD).
        sort_by: Sort field (distressScore, assessedValue, lastSalePrice, yearBuilt, etc.)
        limit: Max rows (default 25, cap 200).
    """
    valid_categories = ["residential", "commercial", "exempt", "agricultural"]
    if category not in valid_categories:
        return json.dumps({"error": f"Invalid category. Must be one of: {valid_categories}"})

    capped = max(1, min(int(limit), 200))
    result = queries.search_properties_by_category(
        category=category,
        limit=capped,
        offset=0,
        sort_by=sort_by,
        sort_dir="desc",
        distress_types=distress_types,
        min_assessed_value=min_value,
        max_assessed_value=max_value,
        address=query,
        county_id=county_id,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_foreclosure_stats(
    days_ahead: int = 30,
    county_id: str = "hamilton",
) -> str:
    """Get summary counts of foreclosure auctions by status.

    Args:
        days_ahead: Look ahead this many days (default 30, cap 365).
        county_id: County identifier (default 'hamilton').
    """
    capped_days = max(0, min(int(days_ahead), 365))
    return json.dumps({
        "at_risk": queries.count_foreclosures(status="at_risk", days_ahead=capped_days),
        "upcoming": queries.count_foreclosures(status="upcoming", days_ahead=capped_days),
        "active": queries.count_foreclosures(status="active", days_ahead=capped_days),
    }, indent=2)


@mcp.tool()
def get_property_appraisal(
    parcel_id: str,
    radius: Optional[float] = None,
    min_comps: int = 5,
    county_id: str = "hamilton",
) -> str:
    """Get property appraisal (CMV + ARV) using comparable sales analysis.

    Uses the properties database as a comp pool — finds comparable sales
    within an adaptive radius, weights by distance and time decay, then
    calculates Current Market Value (CMV) and After Repair Value (ARV).

    Args:
        parcel_id: County parcel ID (exact match).
        radius: Search radius in miles (default: adaptive, starts 0.25mi, expands to 5mi).
        min_comps: Minimum comparable sales needed (default: 5).
        county_id: County identifier (default 'hamilton').
    """
    from core import appraisal
    result = appraisal.full_appraisal(
        parcel_id,
        radius_miles=radius or 5.0,
        min_comps=min_comps,
        county_id=county_id,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def meilisearch_search(query: str, limit: int = 25) -> str:
    """Full-text search across all properties using Meilisearch."""
    results = ms.meilisearch_search(q=query, limit=limit)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_market_context(year: Optional[str] = "latest") -> str:
    """Get market scoring context for deal analysis."""
    context = market_scoring.get_market_context(year=year)
    return json.dumps(context, indent=2, default=str)


@mcp.tool()
def get_comps(parcel_id: str, radius: Optional[float] = None, sqft_tolerance: float = 0.20, months: int = 24) -> str:
    """Get raw comparable sales data for a property."""
    comps = appraisal.get_comps(parcel_id, radius=radius, sqft_tolerance=sqft_tolerance, months=months)
    return json.dumps(comps, indent=2, default=str)


@mcp.tool()
def list_counties() -> str:
    """List all available counties with property counts."""
    counties = queries.count_counties()
    return json.dumps(counties, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
