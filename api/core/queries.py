"""
Shared DB query helpers for properties, distress signals, and foreclosures.
Both the FastAPI routes (core/properties.py) and the MCP server (mcp_server.py)
call into this module so they cannot drift.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from . import market as market_scoring

def _db_dsn() -> str:
    """Resolve DATABASE_URL at call time — loud error if unset (no default DSN)."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set — queries require an explicit DSN "
            "(export it, e.g. from the repo .env; no default is shipped)."
        )
    return dsn


def get_conn():
    return psycopg2.connect(_db_dsn())


SORT_MAP = {
    "distressScore":  "distress_count",
    "assessedValue":  "p.assessed_value",
    "lastSalePrice":  "p.last_sale_price",
    "lastSaleDate":   "p.last_sale_date",
    "equityEstimate": "(COALESCE(p.assessed_value, 0) - COALESCE(p.last_sale_price, 0))",
    "yearBuilt":      "p.year_built",
    "buildingSqft":   "p.building_sqft",
    "address":        "p.address",
    "dealScore":      "COALESCE(p.distress_score, 0)",
    "marketAdjustedDealScore": "COALESCE(p.distress_score, 0)",
}

FLAG_TO_SIGNAL = {
    "preForeclosureFlag": "pre_foreclosure",
    "taxDelinquentFlag":  "tax_delinquent",
    "codeViolationFlag":  "code_violation",
    "probateFlag":        "probate",
    "fsboFlag":           "fsbo",
    "vacancyHeuristic":   "vacancy",
}

VALID_SIGNAL_TYPES = set(FLAG_TO_SIGNAL.values())

KNOWN_COUNTIES = {
    "hamilton": {"name": "Hamilton County", "state": "TN", "region": "Chattanooga"},
}


def county_slug(county: Optional[str]) -> str:
    """Return the stable frontend slug for a county display value."""
    if not county:
        return ""
    slug = str(county).strip().lower().replace("-", "_").replace(" ", "_")
    if slug.endswith("_county"):
        slug = slug[:-7]
    return slug


def county_name_from_slug(slug: str) -> str:
    words = [w for w in slug.replace("-", "_").split("_") if w]
    return " ".join(w.title() for w in words) + " County" if words else "Unknown County"


def property_category_for_row(row: dict) -> str:
    existing = row.get("property_category")
    if existing:
        return existing
    lot_size = row.get("lot_size_sqft")
    building_sqft = row.get("building_sqft")
    if lot_size is not None and float(lot_size) > 43560:
        return "agricultural"
    if building_sqft is None:
        return "exempt"
    if float(building_sqft) > 5000:
        return "commercial"
    return "residential"


def to_iso(val) -> Optional[str]:
    if val is None:
        return None
    return val.isoformat() if hasattr(val, "isoformat") else str(val)


def to_float(val) -> Optional[float]:
    """Convert Decimal/numeric values to float for JSON serialization."""
    if val is None:
        return None
    return float(val)


def row_to_property(row: dict) -> dict:
    signal_set = set(row.get("signal_types") or [])
    assessed = float(row.get("assessed_value") or 0)
    sale_price = float(row.get("last_sale_price") or 0)

    flags = {
        "preForeclosureFlag": "pre_foreclosure" in signal_set,
        "taxDelinquentFlag":  "tax_delinquent" in signal_set,
        "codeViolationFlag":  "code_violation" in signal_set,
        "probateFlag":        "probate" in signal_set,
        "fsboFlag":           "fsbo" in signal_set,
        "vacancyHeuristic":   "vacancy" in signal_set,
    }
    distress_score = sum(1 for v in flags.values() if v)

    # Use DB column values when available, fall back to signal-based
    is_vacant = flags["vacancyHeuristic"]
    is_tax_delinquent = bool(row.get("is_tax_delinquent")) if row.get("is_tax_delinquent") is not None else flags["taxDelinquentFlag"]
    db_distress_score = row.get("distress_score")
    if db_distress_score is not None and float(db_distress_score) > 0:
        final_distress_score = int(float(db_distress_score))
    else:
        final_distress_score = distress_score

    deal_score = int(row.get("deal_score")) if row.get("deal_score") is not None else 0

    property_data = {
        "id":                  row["id"],
        "countyId":            county_slug(row.get("county_id") or row.get("county")),
        "parcelId":            row["parcel_id"],
        "address":             row["address"],
        "city":                row.get("city", "Chattanooga"),
        "state":               row.get("state", "TN"),
        "postalCode":          row.get("postal_code", ""),
        "propertyCategory":    property_category_for_row(row),
        "currentUse":          row.get("current_use"),
        "geocoords":           {"lat": to_float(row["lat"]), "lng": to_float(row["lng"])} if row.get("lat") is not None and row.get("lng") is not None else None,
        "lotSizeSqft":         to_float(row.get("lot_size_sqft")),
        "buildingSqft":        to_float(row.get("building_sqft")),
        "yearBuilt":           row.get("year_built"),
        "bedrooms":            row.get("bedrooms"),
        "bathrooms":           row.get("bathrooms"),
        "lastSalePrice":       to_float(row.get("last_sale_price")),
        "lastSaleDate":        to_iso(row.get("last_sale_date")),
        "assessedValue":       to_float(row.get("assessed_value")),
        "taxAmount":           to_float(row.get("tax_amount")),
        "ownerOfRecord":       row.get("owner_of_record"),
        "ownerMailingAddress": row.get("owner_mailing_address"),
        "ownerOccupant":       bool(row.get("owner_occupant", False)),
        "isVacant":            is_vacant,
        "isTaxDelinquent":     is_tax_delinquent,
        "equityEstimate":      (assessed - sale_price) if assessed and sale_price else None,
        "distressScore":       final_distress_score,
        "dealScore":           deal_score,
        **flags,
        "sourceProvenance":    {"assessedValue": "hamilton_co_assessor"},
        "lastScrapedAt":       None,
        "createdAt":           to_iso(row.get("created_at")),
        "updatedAt":           to_iso(row.get("updated_at")),
    }
    property_data.update(market_scoring.market_fields_for_score(deal_score))
    return property_data


def _build_property_filters(
    address: Optional[str],
    parcel_id: Optional[str],
    min_assessed_value: Optional[float],
    max_assessed_value: Optional[float],
    distress_types: Optional[list[str]],
    property_category: Optional[str] = None,
    county_id: Optional[str] = "hamilton",
    use_county_alias: bool = False,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if county_id:
        if use_county_alias:
            conditions.append("p.county ILIKE %s")
        else:
            conditions.append("p.county_id = %s")
        params.append(county_id)
    if address:
        conditions.append("p.address ILIKE %s")
        params.append(f"%{address}%")
    if parcel_id:
        conditions.append("p.parcel_id ILIKE %s")
        params.append(f"%{parcel_id}%")
    if min_assessed_value is not None:
        conditions.append("p.assessed_value >= %s")
        params.append(min_assessed_value)
    if max_assessed_value is not None:
        conditions.append("p.assessed_value <= %s")
        params.append(max_assessed_value)
    if property_category:
        # Precedence MUST mirror property_category_for_row / the SQL views:
        # agricultural wins first (any lot > 1 acre), then exempt (no
        # building), then commercial (building > 5000 sqft), else residential.
        # Without the agricultural guard below, a 10-acre mini-warehouse
        # matched residential AND commercial, inflating category counts.
        if property_category == "residential":
            conditions.append(
                "p.building_sqft IS NOT NULL AND p.building_sqft <= 5000"
                " AND (p.lot_size_sqft IS NULL OR p.lot_size_sqft <= 43560)"
            )
        elif property_category == "commercial":
            conditions.append(
                "p.building_sqft IS NOT NULL AND p.building_sqft > 5000"
                " AND (p.lot_size_sqft IS NULL OR p.lot_size_sqft <= 43560)"
            )
        elif property_category == "exempt":
            conditions.append(
                "p.building_sqft IS NULL"
                " AND (p.lot_size_sqft IS NULL OR p.lot_size_sqft <= 43560)"
            )
        elif property_category == "agricultural":
            conditions.append("p.lot_size_sqft IS NOT NULL AND p.lot_size_sqft > 43560")

    if distress_types:
        signal_names = [
            FLAG_TO_SIGNAL.get(t, t) for t in distress_types
            if FLAG_TO_SIGNAL.get(t, t) in VALID_SIGNAL_TYPES
        ]
        # AND logic: each signal gets its own EXISTS, so a property must match ALL selected
        for sname in signal_names:
            conditions.append(
                "EXISTS (SELECT 1 FROM distress_signals ds2 "
                "WHERE ds2.property_id = p.id AND ds2.signal_type::text = %s)"
            )
            params.append(sname)

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where_sql, params


def search_properties(
    *,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "distressScore",
    sort_dir: str = "desc",
    distress_types: Optional[list[str]] = None,
    min_assessed_value: Optional[float] = None,
    max_assessed_value: Optional[float] = None,
    address: Optional[str] = None,
    parcel_id: Optional[str] = None,
    property_category: Optional[str] = None,
    county_id: Optional[str] = None,
    cache: Optional[object] = None,
) -> dict:
    order_col = SORT_MAP.get(sort_by, "distress_count")
    order_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"

    where_sql, params = _build_property_filters(
        address=address,
        parcel_id=parcel_id,
        min_assessed_value=min_assessed_value,
        max_assessed_value=max_assessed_value,
        distress_types=distress_types,
        property_category=property_category,
        county_id=county_id,
    )

    # Build a cache key from the query parameters
    if cache is not None:
        cache_key_parts = [
            f"search:limit={limit}",
            f"offset={offset}",
            f"sort={sort_by}:{sort_dir}",
            f"county={county_id or 'all'}",
        ]
        if distress_types:
            cache_key_parts.append(f"distress={','.join(distress_types)}")
        if address:
            cache_key_parts.append(f"addr={address}")
        if parcel_id:
            cache_key_parts.append(f"parcel={parcel_id}")
        if min_assessed_value is not None:
            cache_key_parts.append(f"min_val={min_assessed_value}")
        if max_assessed_value is not None:
            cache_key_parts.append(f"max_val={max_assessed_value}")
        if property_category:
            cache_key_parts.append(f"cat={property_category}")
        cache_key = "|".join(cache_key_parts)

        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

    base_query = f"""
        WITH distress_agg AS (
            SELECT property_id,
                   array_agg(signal_type::text) AS signal_types,
                   COUNT(*)                     AS distress_count,
                   bool_or(signal_type = 'vacancy')        AS is_vacant,
                   bool_or(signal_type = 'tax_delinquent') AS is_tax_delinquent
            FROM distress_signals
            GROUP BY property_id
        )
        SELECT p.*,
               p.lat AS lat, p.lng AS lng,
               COALESCE(d.signal_types, ARRAY[]::text[]) AS signal_types,
               COALESCE(d.distress_count, 0)              AS distress_count,
               COALESCE(d.is_vacant, false)                AS is_vacant,
               COALESCE(d.is_tax_delinquent, false)       AS is_tax_delinquent,
               COALESCE(
                    CASE 
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1960 THEN 10
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 5
                        ELSE 0
                    END +
                    CASE WHEN d.is_tax_delinquent THEN 15
                         ELSE 0
                    END +
                    CASE WHEN d.is_vacant THEN 20
                     ELSE 0
                    END
                ) AS distress_score
        FROM properties p
        LEFT JOIN distress_agg d ON p.id = d.property_id
        {where_sql}
    """

    count_query = f"""
        WITH distress_agg AS (
            SELECT property_id, COUNT(*) AS distress_count
            FROM distress_signals GROUP BY property_id
        )
        SELECT COUNT(*) AS total
        FROM properties p
        LEFT JOIN distress_agg d ON p.id = d.property_id
        {where_sql}
    """

    data_query = base_query + f"ORDER BY {order_col} {order_dir} NULLS LAST LIMIT %s OFFSET %s"

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(count_query, params)
            total = cur.fetchone()["total"]

            cur.execute(data_query, params + [limit, offset])
            rows = cur.fetchall()
    finally:
        conn.close()

    result = {
        "data": [row_to_property(dict(r)) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }

    # Cache the result
    if cache is not None and cache_key:
        cache.set(cache_key, result, ttl=300)

    return result


def get_property_by_parcel_id(parcel_id: str, county_id: Optional[str] = None, cache: Optional[object] = None) -> Optional[dict]:
    # Build cache key
    cache_key = None
    if cache is not None:
        cache_key = f"property:{parcel_id}:{county_id or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # NB: parcel_id is treated as globally unique here; county_id is
            # NOT part of the predicate (matches long-standing behavior — the
            # old `if county_id:` guard merely skipped execute() when absent,
            # causing UnboundLocalError on row).
            cur.execute("""
                    WITH distress_agg AS (
                        SELECT property_id,
                               array_agg(signal_type::text) AS signal_types,
                               COUNT(*)                     AS distress_count,
                               bool_or(signal_type = 'tax_delinquent') AS is_tax_delinquent
                        FROM distress_signals
                        GROUP BY property_id
                    )
                    SELECT p.*,
                           p.lat AS lat, p.lng AS lng,
                           COALESCE(d.signal_types,  ARRAY[]::text[]) AS signal_types,
                           COALESCE(d.distress_count, 0)              AS distress_count,
                           false                                      AS is_vacant,
                           COALESCE(d.is_tax_delinquent, false)       AS is_tax_delinquent,
                           COALESCE(d.distress_count, 0) * 25         AS distress_score
                    FROM properties p
                    LEFT JOIN distress_agg d ON p.id = d.property_id
                    WHERE p.parcel_id = %s
                """, (parcel_id,))
            row = cur.fetchone()
            # Verified signal timeline — real source attribution per signal
            if row:
                cur.execute("""
                    SELECT signal_type, detected_at, source, source_url, confidence, metadata
                    FROM distress_signals WHERE property_id = %s
                    ORDER BY detected_at DESC
                """, (row["id"],))
                row["signal_timeline"] = [
                    {
                        "type": r["signal_type"],
                        "detectedAt": to_iso(r["detected_at"]),
                        "source": r["source"],
                        "sourceUrl": r["source_url"],
                        "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                        "metadata": r["metadata"],
                    } for r in cur.fetchall()
                ]
    finally:
        conn.close()

    if row:
        result = row_to_property(dict(row))
        result["signalTimeline"] = row.get("signal_timeline") or []
        if cache is not None and cache_key:
            cache.set(cache_key, result, ttl=300)
        return result
    return None


def list_foreclosures(
    *,
    status: str = "at_risk",
    days_ahead: int = 30,
    limit: int = 100,
    county_id: Optional[str] = "hamilton",
) -> list[dict]:
    query = """
        SELECT f.id, f.auction_date, f.auction_location, f.min_bid,
               f.status, f.source, f.auction_url, f.recorded_at,
               p.parcel_id, p.address, p.city, p.state, p.assessed_value
        FROM foreclosures f
        JOIN properties p ON f.property_id = p.id
        WHERE f.status = %s
          AND p.county_id = %s
          AND (f.auction_date IS NULL
               OR f.auction_date <= CURRENT_DATE + (%s || ' days')::interval)
        ORDER BY f.auction_date ASC NULLS LAST
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (status, county_id, str(days_ahead), limit))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "id":              r["id"],
            "parcelId":        r["parcel_id"],
            "address":         r["address"],
            "city":            r.get("city"),
            "state":           r.get("state"),
            "auctionDate":     to_iso(r.get("auction_date")),
            "auctionLocation": r.get("auction_location"),
            "minBid":          r.get("min_bid"),
            "status":          r.get("status"),
            "source":          r.get("source"),
            "auctionUrl":      r.get("auction_url"),
            "assessedValue":   r.get("assessed_value"),
            "recordedAt":      to_iso(r.get("recorded_at")),
        }
        for r in rows
    ]


def get_distress_signals_for_parcel(parcel_id: str, county_id: Optional[str] = "hamilton") -> list[dict]:
    query = """
        SELECT ds.signal_type, ds.detected_at,
               NULL::text AS source_url, NULL::text AS notes,
               ds.source, ds.severity, ds.confidence, ds.metadata
        FROM distress_signals ds
        JOIN properties p ON ds.property_id = p.id
        WHERE p.parcel_id = %s AND p.county_id = %s
        ORDER BY ds.detected_at DESC
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (parcel_id, county_id))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "signalType": r.get("signal_type"),
            "detectedAt": to_iso(r.get("detected_at")),
            "source":     r.get("source"),
            "sourceUrl":  r.get("source_url"),
            "severity":   r.get("severity"),
            "confidence": r.get("confidence"),
            "notes":      r.get("notes"),
            "metadata":   r.get("metadata"),
        }
        for r in rows
    ]


def list_distressed(
    *,
    signal_type: Optional[str] = None,
    limit: int = 500,
    county_id: Optional[str] = None,
) -> list[dict]:
    """Return all distressed properties with lat/lng + signal data for map rendering.

    Each row is one property-signal pair so the frontend can render multiple
    markers per property if it has multiple signal types. Only returns properties
    that have coordinates (lat/lng is not null).
    """
    conditions: list[str] = []
    params: list[Any] = []

    if county_id:
        conditions.append("p.county_id = %s")
        params.append(county_id)
    if signal_type:
        conditions.append("ds.signal_type = %s")
        params.append(signal_type)
    conditions.append("p.lat IS NOT NULL AND p.lng IS NOT NULL")

    where_sql = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT p.id AS property_id, p.parcel_id, p.address, p.city, p.state,
               p.postal_code,
               p.lat AS lat, p.lng AS lng,
               ds.signal_type, ds.severity, ds.confidence,
               ds.detected_at AS last_updated
        FROM distress_signals ds
        JOIN properties p ON ds.property_id = p.id
        {where_sql}
        ORDER BY p.address, ds.signal_type
        LIMIT %s
    """

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params + [limit])
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "propertyId": r["property_id"],
            "parcelId":   r["parcel_id"],
            "address":    r["address"],
            "city":       r.get("city"),
            "state":      r.get("state"),
            "postalCode": r.get("postal_code"),
            "latitude":   r["lat"],
            "longitude":  r["lng"],
            "signalType": r.get("signal_type"),
            "severity":   r.get("severity"),
            "confidence": r.get("confidence"),
            "lastUpdated": to_iso(r.get("last_updated")),
        }
        for r in rows
    ]


# ── Health/statistics helpers ──

def count_properties(county_id: Optional[str] = "hamilton") -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM properties p WHERE p.county_id = %s", (county_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def count_distressed(county_id: Optional[str] = "hamilton") -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT ds.property_id) FROM distress_signals ds JOIN properties p ON ds.property_id = p.id WHERE p.county_id = %s",
                (county_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def count_fsbo(county_id: Optional[str] = "hamilton") -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM fsbo_listings WHERE county_id = %s", (county_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def count_pre_foreclosure(county_id: Optional[str] = "hamilton") -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT ds.property_id) FROM distress_signals ds "
                "JOIN properties p ON ds.property_id = p.id "
                "WHERE ds.signal_type = 'pre_foreclosure' AND p.county_id = %s",
                (county_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def count_foreclosures(*, status: str = "at_risk", days_ahead: int = 30, county_id: Optional[str] = "hamilton") -> int:
    """Count foreclosure records matching a status and time window."""
    if status == "active":
        query = "SELECT count(*) FROM foreclosures f JOIN properties p ON f.property_id = p.id WHERE f.status = %s AND p.county_id = %s AND f.auction_date BETWEEN CURRENT_DATE AND CURRENT_DATE + %s"
        params = (status, county_id, days_ahead)
    else:
        query = "SELECT count(*) FROM foreclosures f JOIN properties p ON f.property_id = p.id WHERE f.status = %s AND p.county_id = %s AND f.auction_date > CURRENT_DATE AND f.auction_date <= CURRENT_DATE + %s"
        params = (status, county_id, days_ahead)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def count_tax_delinquent(county_id: Optional[str] = "hamilton") -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT ds.property_id) FROM distress_signals ds "
                "JOIN properties p ON ds.property_id = p.id "
                "WHERE ds.signal_type = 'tax_delinquent' AND p.county_id = %s",
                (county_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def last_scraped_at(county_id: Optional[str] = "hamilton") -> Optional[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(ds.detected_at) FROM distress_signals ds JOIN properties p ON ds.property_id = p.id WHERE p.county_id = %s",
                (county_id,),
            )
            val = cur.fetchone()[0]
            return to_iso(val)
    finally:
        conn.close()


def compare_properties(parcel_ids: list[str], county_id: Optional[str] = "hamilton") -> list[dict]:
    """Fetch multiple properties by parcel_id for side-by-side comparison."""
    if not parcel_ids:
        return []

    if len(parcel_ids) > 10:
        raise ValueError("compare_properties: max 10 parcel IDs allowed")

    placeholders = ",".join(["%s"] * len(parcel_ids))
    query = f"""
        WITH distress_agg AS (
            SELECT property_id,
                   array_agg(signal_type::text) AS signal_types,
                   COUNT(*)                     AS distress_count,
                   bool_or(signal_type = 'vacancy')        AS is_vacant,
                   bool_or(signal_type = 'tax_delinquent') AS is_tax_delinquent
            FROM distress_signals ds
            WHERE EXISTS (SELECT 1 FROM properties p WHERE p.id = ds.property_id AND p.county_id = %s)
            GROUP BY property_id
        )
        SELECT p.id, p.parcel_id, p.county_id AS county, p.address, p.city, p.state, p.postal_code,
               p.lot_size_sqft, p.building_sqft, p.year_built,
               p.bedrooms, p.bathrooms, p.last_sale_price, p.last_sale_date,
               p.assessed_value, p.tax_amount, p.owner_of_record,
               p.owner_mailing_address, p.owner_occupant,
               p.created_at, p.updated_at, COALESCE(p.deal_score, 0) AS deal_score,
               COALESCE(d.signal_types,  ARRAY[]::text[]) AS signal_types,
               COALESCE(d.distress_count, 0)              AS distress_count,
               COALESCE(d.is_vacant, false)              AS is_vacant,
               CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                    AND p.assessed_value > p.last_sale_price * 1.5 THEN true
                    ELSE false END AS is_tax_delinquent,
               COALESCE(
                    CASE
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1960 THEN 10
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 5
                        ELSE 0
                    END +
                    CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                         AND p.assessed_value > p.last_sale_price * 1.5 THEN 15
                         ELSE 0
                    END +
                    0,
                    0
               ) AS distress_score,
               p.lat AS lat,
               p.lng AS lng
        FROM properties p
        LEFT JOIN distress_agg d ON p.id = d.property_id
        WHERE p.parcel_id IN ({placeholders}) AND p.county_id = %s
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (county_id,) + tuple(parcel_ids) + (county_id,))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [row_to_property(dict(r)) for r in rows]


def list_fsbo_listings(limit: int = 100, offset: int = 0, county_id: Optional[str] = "hamilton") -> dict:
    """List FSBO listings from Craigslist with optional property matches."""
    query = """
        SELECT fl.id, fl.source, fl.listing_url, fl.listing_price, fl.description,
               fl.status, fl.days_on_market, fl.scraped_at, fl.listed_at,
               fl.property_id,
               p.parcel_id, p.address AS matched_address
        FROM fsbo_listings fl
        LEFT JOIN properties p ON fl.property_id = p.id
        WHERE fl.county_id = %s
        ORDER BY fl.scraped_at DESC
        LIMIT %s OFFSET %s
    """
    count_query = "SELECT count(*) FROM fsbo_listings WHERE county_id = %s"

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (county_id, limit, offset))
            rows = cur.fetchall()
            cur.execute(count_query, (county_id,))
            total = cur.fetchone()["count"]
    finally:
        conn.close()

    return {
        "data": [
            {
                "id": r["id"],
                "source": r["source"],
                "listingUrl": r.get("listing_url"),
                "listingPrice": float(r["listing_price"]) if r["listing_price"] else None,
                "description": r.get("description"),
                "status": r.get("status"),
                "daysOnMarket": r.get("days_on_market"),
                "scrapedAt": to_iso(r.get("scraped_at")),
                "listedAt": to_iso(r.get("listed_at")),
                "matchedPropertyId": r.get("property_id"),
                "matchedParcelId": r.get("parcel_id"),
                "matchedAddress": r.get("matched_address"),
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# NB: currently unwired — no collector or endpoint calls this yet, but it is
# the only populator of p.deal_score (kept intentionally).
def calculate_deal_scores(county_id: Optional[str] = "hamilton") -> int:
    """Calculate deal_score for all properties in a county and persist to DB.

    Scoring (0-100):
    - Equity estimate: up to 30 points (normalized by 75th percentile)
    - Distress score: up to 30 points (5 per signal, max 6)
    - Vacancy: 10 points if vacant
    - Absentee owner: 20 points if NOT owner-occupied (easier to sell to)
    - Age bonus: 10 points if built before 1980
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Use 75th percentile of positive equity for normalization
            cur.execute("""
                SELECT percentile_disc(0.75) WITHIN GROUP (ORDER BY assessed_value - last_sale_price)
                FROM properties p
                WHERE p.county_id = %s
                  AND assessed_value IS NOT NULL AND last_sale_price IS NOT NULL
                  AND assessed_value > last_sale_price
            """, (county_id,))
            p75 = cur.fetchone()[0]
            equity_norm = float(p75) if p75 else 1

            # Compute distress counts and vacancy from distress_signals
            cur.execute("""
                WITH distress_agg AS (
                    SELECT p.id AS property_id,
                           COUNT(ds.id) AS distress_count,
                           bool_or(ds.signal_type = 'vacancy') AS is_vacant
                    FROM properties p
                    LEFT JOIN distress_signals ds ON ds.property_id = p.id
                    WHERE p.county_id = %s
                    GROUP BY p.id
                )
                UPDATE properties
                SET deal_score = LEAST(100,
                    -- Equity: 0-30 points (normalized by 75th percentile)
                    CASE WHEN assessed_value IS NOT NULL AND last_sale_price IS NOT NULL
                         AND assessed_value > last_sale_price
                         THEN ROUND(LEAST((assessed_value - last_sale_price)::numeric / NULLIF(%s::numeric, 0), 1) * 30, 0)
                         ELSE 0 END
                    -- Distress: 0-30 points (5 per signal)
                    + LEAST(COALESCE(da.distress_count, 0) * 5, 30)
                    -- Vacancy: 10 points
                    + CASE WHEN COALESCE(da.is_vacant, false) THEN 10 ELSE 0 END
                    -- Absentee owner: 20 points (easier to sell to)
                    + 0
                    -- Age bonus: 10 points if built before 1980
                    + CASE WHEN year_built IS NOT NULL AND year_built < 1980 THEN 10 ELSE 0 END
                )
                FROM distress_agg da
                WHERE properties.id = da.property_id
            """, (county_id, equity_norm))
            updated = cur.rowcount
            conn.commit()
            return updated
    finally:
        conn.close()


def count_properties_by_category(county_id: Optional[str] = "hamilton") -> list[dict]:
    """Count properties by category, scoped to a county.
    
    Categories derived from building_sqft:
    - residential: building_sqft <= 5000
    - commercial: building_sqft > 5000
    - exempt: building_sqft IS NULL (vacant land, etc.)
    - agricultural: derived from lot_size_sqft > 43560 (1 acre)
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    CASE 
                        WHEN lot_size_sqft IS NOT NULL AND lot_size_sqft > 43560 THEN 'agricultural'
                        WHEN building_sqft IS NULL THEN 'exempt'
                        WHEN building_sqft > 5000 THEN 'commercial'
                        ELSE 'residential'
                    END AS category,
                    COUNT(*) AS count
                FROM properties p
                WHERE p.county_id = %s
                GROUP BY category
                ORDER BY count DESC
            """, (county_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def count_counties() -> list[dict]:
    """Return available counties with property counts and metadata."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.county_id AS county,
                       COUNT(DISTINCT p.id) AS property_count,
                       COUNT(DISTINCT CASE WHEN ds.property_id IS NOT NULL THEN ds.property_id END) AS distressed_count
                FROM properties p
                LEFT JOIN distress_signals ds ON p.id = ds.property_id
                GROUP BY p.county_id
                ORDER BY property_count DESC
            """)
            rows = cur.fetchall()
        result = []
        for r in rows:
            slug = county_slug(r["county"])
            metadata = KNOWN_COUNTIES.get(slug, {})
            result.append({
                "county_id": slug,
                "name": metadata.get("name", county_name_from_slug(slug)),
                "state": metadata.get("state", "??"),
                "region": metadata.get("region", ""),
                "propertyCount": int(r["property_count"]),
                "distressedCount": int(r["distressed_count"]),
            })
        return result
    finally:
        conn.close()


# ── View-based queries ──

CATEGORY_VIEWS = {
    "residential": "residential_properties",
    "commercial": "commercial_properties",
    "exempt": "exempt_properties",
    "agricultural": "agricultural_properties",
}


def search_properties_by_category(
    category: str,
    *,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "distressScore",
    sort_dir: str = "desc",
    distress_types: Optional[list[str]] = None,
    min_assessed_value: Optional[float] = None,
    max_assessed_value: Optional[float] = None,
    address: Optional[str] = None,
    parcel_id: Optional[str] = None,
    county_id: Optional[str] = "hamilton",
) -> dict:
    """Search within a specific property category, scoped to a county."""
    if category not in CATEGORY_VIEWS:
        raise ValueError(f"Invalid category: {category}. Must be one of: {list(CATEGORY_VIEWS.keys())}")

    view_name = CATEGORY_VIEWS[category]
    order_col = SORT_MAP.get(sort_by, "distress_count")
    order_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"

    where_sql, params = _build_property_filters(
        address=address,
        parcel_id=parcel_id,
        min_assessed_value=min_assessed_value,
        max_assessed_value=max_assessed_value,
        distress_types=distress_types,
        county_id=county_id,
        use_county_alias=False,
    )

    base_query = f"""
        WITH distress_agg AS (
            SELECT property_id,
                   array_agg(signal_type::text) AS signal_types,
                   COUNT(*)                     AS distress_count,
                   bool_or(signal_type = 'vacancy')        AS is_vacant,
                   bool_or(signal_type = 'tax_delinquent') AS is_tax_delinquent
            FROM distress_signals
            GROUP BY property_id
        )
        SELECT p.id, p.parcel_id, p.county_id, p.address, p.city, p.state, p.postal_code,
               p.lot_size_sqft, p.building_sqft, p.year_built,
               p.bedrooms, p.bathrooms, p.last_sale_price, p.last_sale_date,
               p.assessed_value, p.tax_amount, p.owner_of_record,
               p.owner_mailing_address, p.owner_occupant,
               p.created_at, p.updated_at, COALESCE(p.deal_score, 0) AS deal_score,
               COALESCE(d.signal_types,  ARRAY[]::text[]) AS signal_types,
               COALESCE(d.distress_count, 0)              AS distress_count,
               COALESCE(d.is_vacant, false)              AS is_vacant,
               CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                    AND p.assessed_value > p.last_sale_price * 1.5 THEN true
                    ELSE false END AS is_tax_delinquent,
               COALESCE(
                    CASE
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1960 THEN 10
                        WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 5
                        ELSE 0
                    END +
                    CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                         AND p.assessed_value > p.last_sale_price * 1.5 THEN 15
                         ELSE 0
                    END +
                    0,
                    0
               ) AS distress_score,
               (p.geocoords_point)[0] AS lng,
               (p.geocoords_point)[1] AS lat
        FROM {view_name} p
        LEFT JOIN distress_agg d ON p.id = d.property_id
        {where_sql}
    """

    count_query = f"""
        WITH distress_agg AS (
            SELECT property_id, COUNT(*) AS distress_count
            FROM distress_signals GROUP BY property_id
        )
        SELECT COUNT(*) AS total
        FROM {view_name} p
        LEFT JOIN distress_agg d ON p.id = d.property_id
        {where_sql}
    """

    data_query = base_query + f"ORDER BY {order_col} {order_dir} NULLS LAST LIMIT %s OFFSET %s"

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(count_query, params)
            total = cur.fetchone()["total"]

            cur.execute(data_query, params + [limit, offset])
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "data": [row_to_property(dict(r)) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "category": category,
    }


def get_property_analysis(parcel_id: str, county_id: Optional[str] = None) -> Optional[dict]:
    """Get deal analysis for a single property."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if county_id:
                cur.execute("""
                    WITH prop AS (
                        SELECT p.*,
                            CASE WHEN assessed_value IS NOT NULL AND last_sale_price IS NOT NULL
                                 AND assessed_value > last_sale_price
                                 THEN ROUND(LEAST((assessed_value - last_sale_price)::numeric / NULLIF(assessed_value::numeric, 0), 1) * 30, 0)
                                 ELSE 0 END AS equity_score,
                            COALESCE(
                                CASE
                                    WHEN year_built IS NOT NULL AND year_built < 1960 THEN 10
                                    WHEN year_built IS NOT NULL AND year_built < 1980 THEN 5
                                    ELSE 0
                                END +
                                CASE WHEN last_sale_price IS NOT NULL AND last_sale_price > 0
                                     AND assessed_value > last_sale_price * 1.5 THEN 15
                                     ELSE 0
                                END +
                                0,
                                0
                            ) AS derived_distress_score
                        FROM properties p
                        WHERE p.parcel_id = %s AND p.county_id = %s
                    ),
                    sig AS (
                        SELECT bool_or(signal_type = 'vacancy') AS is_vacant
                        FROM distress_signals WHERE property_id = (SELECT id FROM properties WHERE parcel_id = %s AND county_id = %s)
                    )
                    SELECT p.parcel_id, p.address, COALESCE(p.deal_score, 0) AS deal_score, p.derived_distress_score AS distress_score, p.owner_occupant, p.year_built,
                        p.assessed_value, p.last_sale_price,
                        p.equity_score,
                        LEAST(COALESCE(p.derived_distress_score, 0) * 5, 30) AS distress_score_points,
                        COALESCE(s.is_vacant, false) AS is_vacant,
                        0 AS absentee_score,
                        CASE WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 10 ELSE 0 END AS age_score
                    FROM prop p, sig s
                """, (parcel_id, county_id, parcel_id, county_id))
            else:
                cur.execute("""
                    WITH prop AS (
                        SELECT p.*,
                            CASE WHEN assessed_value IS NOT NULL AND last_sale_price IS NOT NULL
                                 AND assessed_value > last_sale_price
                                 THEN ROUND(LEAST((assessed_value - last_sale_price)::numeric / NULLIF((
                                     SELECT percentile_disc(0.75) WITHIN GROUP (ORDER BY assessed_value - last_sale_price)
                                     FROM properties
                                     WHERE assessed_value IS NOT NULL AND last_sale_price IS NOT NULL
                                       AND assessed_value > last_sale_price
                                 )::numeric, 0), 1), 1) * 30, 0)
                                 ELSE 0 END AS equity_score,
                            COALESCE(
                                CASE
                                    WHEN year_built IS NOT NULL AND year_built < 1960 THEN 10
                                    WHEN year_built IS NOT NULL AND year_built < 1980 THEN 5
                                    ELSE 0
                                END +
                                CASE WHEN last_sale_price IS NOT NULL AND last_sale_price > 0
                                     AND assessed_value > last_sale_price * 1.5 THEN 15
                                     ELSE 0
                                END +
                                0,
                                0
                            ) AS derived_distress_score
                        FROM properties p
                        WHERE p.parcel_id = %s
                    ),
                    sig AS (
                        SELECT bool_or(signal_type = 'vacancy') AS is_vacant
                        FROM distress_signals WHERE property_id = (SELECT id FROM properties WHERE parcel_id = %s)
                    )
                    SELECT p.parcel_id, p.address, COALESCE(p.deal_score, 0) AS deal_score, p.derived_distress_score AS distress_score, p.owner_occupant, p.year_built,
                        p.assessed_value, p.last_sale_price,
                        p.equity_score,
                        LEAST(COALESCE(p.derived_distress_score, 0) * 5, 30) AS distress_score_points,
                        COALESCE(s.is_vacant, false) AS is_vacant,
                        0 AS absentee_score,
                        CASE WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 10 ELSE 0 END AS age_score
                    FROM prop p, sig s
                """, (parcel_id, parcel_id))
            row = cur.fetchone()
            if not row:
                return None

            deal_score = int(row["deal_score"] or 0)
            analysis = {
                "parcelId": row["parcel_id"],
                "address": row["address"],
                "dealScore": deal_score,
                "breakdown": {
                    "equityEstimate": float(row["assessed_value"] or 0) - float(row["last_sale_price"] or 0),
                    "distressScore": int(row["distress_score"] or 0),
                    "isVacant": row["is_vacant"],
                    "isOwnerOccupant": row["owner_occupant"],
                    "yearBuilt": row["year_built"],
                    "assessedValue": float(row["assessed_value"] or 0),
                    "lastSalePrice": float(row["last_sale_price"] or 0),
                },
                "scores": {
                    "equity": int(row["equity_score"] or 0),
                    "distress": int(row["distress_score_points"] or 0),
                    "vacancy": int(10 if row["is_vacant"] else 0),
                    "absentee": int(row["absentee_score"] or 0),
                    "age": int(row["age_score"] or 0),
                }
            }
            analysis.update(market_scoring.market_fields_for_score(deal_score, include_context=True))
            return analysis
    finally:
        conn.close()


def get_top_deals(limit: int = 50, min_score: int = 50, county_id: Optional[str] = None) -> dict:
    """Get top deals, sorted by market-adjusted score descending."""
    context = market_scoring.get_market_context("latest")
    score_floor = market_scoring.local_floor_for_adjusted_min(min_score, context)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if county_id:
                where_sql = "WHERE p.county_id = %s AND p.derived_distress_score >= %s"
                params = [county_id, score_floor]
            else:
                where_sql = "WHERE p.derived_distress_score >= %s"
                params = [score_floor]

            scored_cte = """
                scored_properties AS (
                    SELECT p.*,
                           COALESCE(
                                CASE
                                    WHEN p.year_built IS NOT NULL AND p.year_built < 1960 THEN 10
                                    WHEN p.year_built IS NOT NULL AND p.year_built < 1980 THEN 5
                                    ELSE 0
                                END +
                                CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                                     AND p.assessed_value > p.last_sale_price * 1.5 THEN 15
                                     ELSE 0
                                END +
                                0,
                                0
                           ) AS derived_distress_score
                    FROM properties p
                )
            """

            query = f"""
                WITH {scored_cte},
                distress_agg AS (
                    SELECT property_id,
                           array_agg(signal_type::text) AS signal_types,
                           COUNT(*)                     AS distress_count,
                           bool_or(signal_type = 'vacancy')         AS is_vacant,
                           bool_or(signal_type = 'tax_delinquent')  AS is_tax_delinquent,
                           bool_or(signal_type = 'pre_foreclosure') AS pre_foreclosure_flag,
                           bool_or(signal_type = 'code_violation')  AS code_violation_flag,
                           bool_or(signal_type = 'probate')         AS probate_flag,
                           bool_or(signal_type = 'fsbo')            AS fsbo_flag
                    FROM distress_signals
                    GROUP BY property_id
                )
                SELECT p.id, p.parcel_id, p.county_id AS county, p.address, p.city, p.state, p.postal_code,
                       p.lot_size_sqft, p.building_sqft, p.year_built,
                       p.bedrooms, p.bathrooms, p.last_sale_price, p.last_sale_date,
                       p.assessed_value, p.tax_amount, p.owner_of_record,
                       p.owner_mailing_address, p.owner_occupant,
                       p.created_at, p.updated_at,
                       COALESCE(NULLIF(p.deal_score, 0), p.derived_distress_score) AS deal_score,
                       p.derived_distress_score AS distress_score,
                       COALESCE(d.signal_types,  ARRAY[]::text[]) AS signal_types,
                       COALESCE(d.distress_count, 0)              AS distress_count,
                       COALESCE(d.is_vacant, false)              AS is_vacant,
                       CASE WHEN p.last_sale_price IS NOT NULL AND p.last_sale_price > 0
                            AND p.assessed_value > p.last_sale_price * 1.5 THEN true
                            ELSE false END AS is_tax_delinquent,
                       COALESCE(d.pre_foreclosure_flag, false)    AS pre_foreclosure_flag,
                       COALESCE(d.code_violation_flag, false)     AS code_violation_flag,
                       COALESCE(d.probate_flag, false)            AS probate_flag,
                       COALESCE(d.fsbo_flag, false)               AS fsbo_flag,
                       p.lat AS lat, p.lng AS lng,
                       (p.assessed_value - p.last_sale_price) AS equity_estimate
                FROM scored_properties p
                LEFT JOIN distress_agg d ON p.id = d.property_id
                {where_sql}
                ORDER BY p.derived_distress_score DESC, p.parcel_id ASC
                LIMIT %s
            """

            count_query = f"""
                WITH {scored_cte}
                SELECT COUNT(*) AS total
                FROM scored_properties p
                {where_sql}
            """

            cur.execute(query, params + [limit])
            rows = cur.fetchall()

            cur.execute(count_query, params)
            total = cur.fetchone()["total"]

            items = []
            for r in rows:
                items.append({
                    "parcelId": r["parcel_id"],
                    "countyId": county_slug(r.get("county")),
                    "address": r["address"],
                    "city": r["city"],
                    "state": r["state"],
                    "postalCode": r["postal_code"],
                    "dealScore": int(r["deal_score"] or 0),
                    "equityEstimate": float(r["assessed_value"] or 0) - float(r["last_sale_price"] or 0),
                    "assessedValue": float(r["assessed_value"] or 0),
                    "lastSalePrice": float(r["last_sale_price"] or 0),
                    "lastSaleDate": r["last_sale_date"].isoformat() if r["last_sale_date"] else None,
                    "yearBuilt": r["year_built"],
                    "buildingSqft": r["building_sqft"],
                    "lotSizeSqft": r["lot_size_sqft"],
                    "bedrooms": r["bedrooms"],
                    "bathrooms": r["bathrooms"],
                    "distressScore": int(r["distress_score"] or 0),
                    "isVacant": r["is_vacant"],
                    "isOwnerOccupant": r["owner_occupant"],
                    "isTaxDelinquent": r["is_tax_delinquent"],
                    "preForeclosureFlag": r["pre_foreclosure_flag"],
                    "codeViolationFlag": r["code_violation_flag"],
                    "probateFlag": r["probate_flag"],
                    "fsboFlag": r["fsbo_flag"],
                    "ownerOfRecord": r["owner_of_record"],
                    "lat": float(r["lat"]) if r["lat"] else None,
                    "lng": float(r["lng"]) if r["lng"] else None,
                })

            for item in items:
                item.update(market_scoring.market_fields_for_score(item.get("dealScore"), context))
            items = [i for i in items if i["marketAdjustedDealScore"] >= min_score]
            total = len(items)  # recount AFTER the score filter — pre-filter total overcounted
            items.sort(key=lambda i: (-i["marketAdjustedDealScore"], -int(i.get("dealScore") or 0), str(i.get("parcelId"))))

            return {"data": items, "total": total, "limit": limit, "offset": 0}
    finally:
        conn.close()
