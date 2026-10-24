"""Database access for geocoding: target selection and the single bulk-write.

Replaces the per-script connection handling and the several copies of the temp
table + ``execute_values`` bulk update (some of which omitted ``geocoords_point``
or used the PostGIS ``ST_MakePoint`` that this non-PostGIS DB does not have).
The one writer here always populates the native ``point`` column.
"""
from __future__ import annotations

import psycopg2
import psycopg2.extras

from .config import DB_DSN

# target name -> WHERE predicate (county is parameterized separately)
_TARGET_SQL = {
    "missing": "lat IS NULL",
    "sold": (
        "last_sale_price > 1000 AND building_sqft > 0 "
        "AND last_sale_date > current_date - interval '24 months' AND lat IS NULL"
    ),
    "residential": "property_category = 'residential' AND lat IS NULL",
    "all": "TRUE",
}


def get_conn(dsn: str = DB_DSN):
    return psycopg2.connect(dsn)


def target_predicate(target: str) -> str:
    if target not in _TARGET_SQL:
        raise ValueError(f"unknown target {target!r}; choose from {sorted(_TARGET_SQL)}")
    return _TARGET_SQL[target]


def fetch_targets(conn, county_id: str, target: str, limit: int | None = None) -> list[dict]:
    """Parcels to geocode for the given county/target (need an address)."""
    pred = target_predicate(target)
    sql = (
        "SELECT id, parcel_id, address, city, state, postal_code, building_sqft "
        "FROM properties "
        f"WHERE county_id = %s AND address IS NOT NULL AND address <> '' AND {pred} "
        "ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (county_id,))
        return [dict(r) for r in cur.fetchall()]


def fetch_known_points(conn, county_id: str) -> list[tuple]:
    """Already-geocoded parcels used as interpolation reference points."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT parcel_id, address, lat, lng FROM properties "
            "WHERE county_id = %s AND lat IS NOT NULL "
            "AND address IS NOT NULL AND address <> ''",
            (county_id,),
        )
        return cur.fetchall()


def bulk_update_coords(conn, rows, county_id: str) -> int:
    """Write ``(lat, lng, parcel_id)`` rows, setting native ``geocoords_point``.

    Uses a temp table + ``execute_values`` + a single ``UPDATE … FROM`` join.
    Returns the number of rows updated.
    """
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _geo_batch (lat float, lng float, parcel_id text) ON COMMIT DROP"
        )
        psycopg2.extras.execute_values(cur, "INSERT INTO _geo_batch VALUES %s", rows)
        cur.execute(
            "UPDATE properties p "
            "SET lat = s.lat, lng = s.lng, "
            "    geocoords_point = point(s.lng, s.lat), updated_at = now() "
            "FROM _geo_batch s "
            "WHERE p.parcel_id = s.parcel_id AND p.county_id = %s",
            (county_id,),
        )
        updated = cur.rowcount
    conn.commit()
    return updated
