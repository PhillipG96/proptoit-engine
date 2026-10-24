#!/usr/bin/env python3
"""
Replace Nominatim/Census geocoded coordinates with authoritative parcel centroids
from Hamilton County's ArcGIS REST API (mapsdev.hamiltontn.gov).

The existing geocoder placed many properties at street centroids (wrong).
This script queries the same GIS endpoint used by enrich_gis.py but requests
the parcel polygon geometry, computes its centroid, and writes exact lat/lng.

Usage:
    python3 ~/proptoit/collectors/enrich_gis_coords.py [--dry-run] [--batch-size N]
"""

import argparse
import os
import sys
import time
from urllib.parse import quote

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/proptoit/.env"))
DB_DSN = os.getenv("DATABASE_URL")
if not DB_DSN:
    raise SystemExit("DATABASE_URL environment variable is required")
GIS_URL = "https://mapsdev.hamiltontn.gov/hcwa03/rest/services/Live_Parcels/MapServer/0/query"
BATCH_SIZE = 50
DELAY = 0.5


def poly_centroid(rings: list) -> tuple[float, float] | None:
    """Compute centroid of a polygon from its ArcGIS ring coordinates.

    Uses area-weighted centroid formula for accuracy. Falls back to
    simple vertex average if area calculation fails.
    Each ring is a list of [lng, lat] pairs.
    """
    if not rings:
        return None
    ring = rings[0]  # outer ring
    if len(ring) < 3:
        return None

    # Area-weighted centroid (Shoelace formula)
    area = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    area /= 2.0
    if abs(area) < 1e-12:
        # Degenerate polygon: fall back to vertex average
        lngs = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        return sum(lats) / len(lats), sum(lngs) / len(lngs)

    cx /= (6.0 * area)
    cy /= (6.0 * area)
    return cy, cx  # (lat, lng)


def fetch_centroids(parcel_ids: list[str]) -> dict[str, tuple[float, float]]:
    """Query ArcGIS for a batch of parcel IDs. Returns {tax_map_no: (lat, lng)}."""
    quoted = ", ".join(f"'{pid}'" for pid in parcel_ids)
    params = {
        "where": f"TAX_MAP_NO IN ({quoted})",
        "outFields": "TAX_MAP_NO",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        r = requests.get(GIS_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  GIS error: {e}", file=sys.stderr)
        return {}

    results = {}
    for feature in data.get("features", []):
        tax_map_no = feature.get("attributes", {}).get("TAX_MAP_NO")
        geometry = feature.get("geometry")
        if not tax_map_no or not geometry:
            continue
        rings = geometry.get("rings")
        if not rings:
            continue
        centroid = poly_centroid(rings)
        if centroid:
            results[tax_map_no] = centroid

    return results


import re as _re

_SHORT_NUMERIC = _re.compile(r'^(\d{3})\s+(\d{3}(?:\.\d+)?)$')

def normalize_parcel_id(parcel_id: str) -> str:
    """Normalize parcel ID for GIS TAX_MAP_NO matching.

    Hamilton County GIS uses double-space as separator for short numeric
    parcel IDs (e.g. '049  019') but single-space for map-sheet IDs
    (e.g. '075A A 021'). The assessor DB uses single spaces throughout.
    """
    s = " ".join(parcel_id.split())  # collapse any whitespace first
    m = _SHORT_NUMERIC.match(s)
    if m:
        return f"{m.group(1)}  {m.group(2)}"  # restore double-space
    return s


def parent_parcel_id(parcel_id: str) -> str | None:
    """Strip condo/unit suffix (e.g. '135L B 009 C001' -> '135L B 009').

    Hamilton County condo units are registered as sub-parcels with suffixes
    like C001, L001 that don't appear in the GIS layer. Stripping the last
    token gives the parent parcel which does have geometry.
    """
    import re
    stripped = re.sub(r'\s+[CL]\d+\s*$', '', parcel_id, flags=re.IGNORECASE).strip()
    if stripped != parcel_id:
        return stripped
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print updates without writing DB")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--all", action="store_true", help="Re-geocode all properties, not just clustered ones")
    args = parser.parse_args()

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    if args.all:
        cur.execute("SELECT id, parcel_id, lat, lng FROM properties ORDER BY id")
    else:
        # Target properties that are in mass-geocoded clusters (coordinate bucket with >3 neighbours)
        # plus properties with no coordinates at all
        cur.execute("""
            SELECT id, parcel_id, lat, lng FROM properties
            WHERE lat IS NULL
               OR (
                 ROUND(lat::numeric, 3),
                 ROUND(lng::numeric, 3)
               ) IN (
                 SELECT ROUND(lat::numeric, 3), ROUND(lng::numeric, 3)
                 FROM properties
                 WHERE lat IS NOT NULL
                 GROUP BY ROUND(lat::numeric, 3), ROUND(lng::numeric, 3)
                 HAVING COUNT(*) > 3
               )
            ORDER BY id
        """)

    rows = cur.fetchall()
    print(f"Targeting {len(rows)} properties for GIS coord replacement")

    # Build lookup maps: normalized parcel_id -> db_id, and a parent fallback map
    db_map: dict[str, int] = {}          # tax_map_no -> db_id (primary lookup)
    parent_map: dict[str, list[int]] = {}  # parent_tax_map_no -> [db_ids] (condo fallback)
    for db_id, parcel_id, lat, lng in rows:
        normalized = normalize_parcel_id(parcel_id)
        db_map[normalized] = db_id
        parent = parent_parcel_id(normalized)
        if parent:
            parent_map.setdefault(parent, []).append(db_id)

    # Query set: primary IDs + unique parent IDs not already in primary set
    primary_ids = set(db_map.keys())
    extra_parents = set(parent_map.keys()) - primary_ids
    parcel_ids = list(primary_ids | extra_parents)
    total = len(parcel_ids)
    updated = 0
    no_match = 0

    for start in range(0, total, args.batch_size):
        batch = parcel_ids[start:start + args.batch_size]
        centroids = fetch_centroids(batch)

        for tax_map_no, (lat, lng) in centroids.items():
            # Direct match
            db_id = db_map.get(tax_map_no)
            if db_id is not None:
                print(f"  [{updated+1}] id={db_id} {tax_map_no} -> lat={lat:.6f} lng={lng:.6f}")
                if not args.dry_run:
                    cur.execute(
                        "UPDATE properties SET lat = %s, lng = %s, updated_at = now() WHERE id = %s",
                        (round(lat, 7), round(lng, 7), db_id),
                    )
                updated += 1

            # Condo child parcels that share this parent's centroid
            for child_id in parent_map.get(tax_map_no, []):
                print(f"  [{updated+1}] id={child_id} (child of {tax_map_no}) -> lat={lat:.6f} lng={lng:.6f}")
                if not args.dry_run:
                    cur.execute(
                        "UPDATE properties SET lat = %s, lng = %s, updated_at = now() WHERE id = %s",
                        (round(lat, 7), round(lng, 7), child_id),
                    )
                updated += 1

        missed_in_batch = len(batch) - len(centroids)
        no_match += missed_in_batch
        if missed_in_batch:
            matched_ids = set(centroids.keys())
            for pid in batch:
                if pid not in matched_ids and pid not in extra_parents:
                    print(f"  NO GIS MATCH: {pid}", file=sys.stderr)

        if not args.dry_run:
            conn.commit()

        pct = min(start + args.batch_size, total)
        print(f"Progress: {pct}/{total} parcels queried, {updated} updated so far")
        time.sleep(DELAY)

    cur.close()
    conn.close()

    print(f"\nDone: {updated} updated, {no_match} no GIS match (dry_run={args.dry_run})")


if __name__ == "__main__":
    main()
