#!/usr/bin/env python3
"""Batch geocoder for the LIVE database — Hamilton County, TN.

Writes coordinates to the converged docker Postgres database the API serves from
(``DATABASE_URL``-resolved DSN, ``county_id`` slug, native ``lat``/``lng`` plus ``point`` ``geocoords_point``).

Strategy:
  1. US Census batch geocoder for numbered street addresses — free, no key,
     200 addresses/request, accurate for US streets.
  2. Nominatim fallback for bare names + Census misses, scoped to city/state.

Every result is validated against the Hamilton County bounding box before it is
written. This is the safety net the previous version lacked: it used to strip
"Chattanooga, TN" from the query and let Nominatim match a bare street name
anywhere on Earth, writing coordinates in Texas, California, even Australia.

Usage:
  python3 collectors/geocode/geocoder.py [--limit N] [--batch-size 200]
  python3 collectors/geocode/geocoder.py --dry-run
  python3 collectors/geocode/geocoder.py --no-nominatim   # Census only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psycopg2
import psycopg2.extras
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from collectors.geocoding.addresses import is_numbered, normalize_address
from collectors.geocoding.census import census_batch
from collectors.geocoding.config import CENSUS_BATCH_SIZE
from collectors.geocoding.nominatim import NominatimClient

from dotenv import load_dotenv
load_dotenv()

# API-serving database. Env override matches the running API.
def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn
CHECKPOINT_FILE = os.getenv("GEOCODE_CHECKPOINT", str(Path(__file__).resolve().parents[2] / ".geocode_checkpoint"))
COUNTY_ID = "hamilton"
COUNTY_NAME = "Hamilton County, TN"

# Hamilton County, TN bounding box (lat_min, lat_max, lng_min, lng_max), padded.
# Actual county bounds are ~34.98–35.26 / -85.51–-85.03; we pad slightly.
HAMILTON_BBOX = (34.95, 35.30, -85.55, -85.0)


def in_county(lat: float, lng: float) -> bool:
    """True when (lat, lng) falls inside the Hamilton County bounding box."""
    lat_min, lat_max, lng_min, lng_max = HAMILTON_BBOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def get_pending(conn, limit=None):
    """Properties in this county that still need coordinates (and have an address)."""
    sql = (
        "SELECT id, parcel_id, address, city, state, postal_code "
        "FROM properties "
        "WHERE county_id = %s AND (lat IS NULL OR lng IS NULL) "
        "AND address IS NOT NULL AND address <> '' "
        "ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (COUNTY_ID,))
        return [dict(r) for r in cur.fetchall()]


def bulk_write(conn, accepted: dict[int, tuple[float, float]]) -> int:
    """Write ``{property_id: (lat, lng)}`` to native coordinates and ``geocoords_point``."""
    if not accepted:
        return 0
    rows = [(pid, lat, lng) for pid, (lat, lng) in accepted.items()]
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _geo (id integer, lat float, lng float) ON COMMIT DROP"
        )
        psycopg2.extras.execute_values(cur, "INSERT INTO _geo VALUES %s", rows)
        cur.execute(
            "UPDATE properties p "
            "SET lat = g.lat, lng = g.lng, geocoords_point = point(g.lng, g.lat), "
            "    updated_at = now() "
            "FROM _geo g WHERE p.id = g.id"
        )
        updated = cur.rowcount
    conn.commit()
    return updated


def save_checkpoint(geocoded_count: int, last_id: str | None) -> None:
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(
            {"geocoded_count": geocoded_count, "last_property_id": last_id, "timestamp": time.time()},
            f,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch geocoder (live DB, Census + Nominatim)")
    parser.add_argument("--limit", type=int, default=None, help="cap number of properties")
    parser.add_argument("--batch-size", type=int, default=CENSUS_BATCH_SIZE,
                        help=f"Census rows per request (default {CENSUS_BATCH_SIZE})")
    parser.add_argument("--rate", type=float, default=None,
                        help="seconds between Nominatim requests (ToS: >= 1.0)")
    parser.add_argument("--no-nominatim", action="store_true",
                        help="Census only; skip the Nominatim fallback")
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args(argv)

    print("=" * 64)
    print(f"BATCH GEOCODER — {COUNTY_NAME}  (live DB)")
    print("=" * 64)

    conn = psycopg2.connect(db_dsn())
    try:
        pending = get_pending(conn, limit=args.limit)
        print(f"Pending (no coords, have address): {len(pending)}")
        if not pending:
            print("Nothing to geocode.")
            return

        numbered = [p for p in pending if is_numbered(p["address"])]
        bare = [p for p in pending if not is_numbered(p["address"])]
        print(f"  numbered={len(numbered)}  bare={len(bare)}")

        if args.dry_run:
            print(f"[dry-run] would Census-geocode {len(numbered)} numbered, "
                  f"Nominatim {len(bare)} bare + Census misses. No writes.")
            return

        accepted: dict[int, tuple[float, float]] = {}
        rejected = 0  # results that fell outside the Hamilton County bbox
        misses = 0    # geocoder returned nothing

        # 1. Census batch for numbered addresses. Census echoes back the row id,
        #    so we pass a synthetic integer index and map it back to the parcel.
        indexed = list(enumerate(numbered))
        for i in range(0, len(indexed), args.batch_size):
            chunk = indexed[i:i + args.batch_size]
            req = [
                {"id": gi, "address": p["address"], "city": p.get("city"),
                 "state": p.get("state"), "postal_code": p.get("postal_code")}
                for gi, p in chunk
            ]
            for gi, (lat, lng) in census_batch(req).items():
                p = numbered[gi]
                if in_county(lat, lng):
                    accepted[p["id"]] = (lat, lng)
                else:
                    rejected += 1
            print(f"  census {min(i + args.batch_size, len(indexed))}/{len(indexed)}: "
                  f"{len(accepted)} accepted, {rejected} rejected")

        # 2. Nominatim fallback for bare names + Census misses.
        if not args.no_nominatim:
            need = bare + [p for p in numbered if p["id"] not in accepted]
            if need:
                client = NominatimClient(rate=args.rate) if args.rate else NominatimClient()
                print(f"  Nominatim fallback: {len(need)} addresses "
                      f"(~{client.rate:.1f}s each)…")
                for p in need:
                    coord = client.geocode_address(
                        normalize_address(p["address"]),
                        p.get("city") or "", p.get("state") or "",
                    )
                    if coord is None:
                        misses += 1
                    elif in_county(*coord):
                        accepted[p["id"]] = coord
                    else:
                        rejected += 1

        updated = bulk_write(conn, accepted)
        save_checkpoint(updated, pending[-1]["id"])

        print("-" * 64)
        print(f"Accepted (in-county): {len(accepted)}")
        print(f"Rejected (out-of-county, NOT written): {rejected}")
        print(f"No match: {misses}")
        print(f"DB rows updated: {updated}")
        print("-" * 64)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
