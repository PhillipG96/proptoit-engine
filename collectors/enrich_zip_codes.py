#!/usr/bin/env python3
"""
Reverse Geocode ZIP Codes — physical address ZIPs from lat/lng coordinates.

Uses Nominatim reverse geocoding (free, no API key) to derive ZIP codes
from property coordinates. Updates postal_code field in properties table.

Rate limited to 1 req/sec per Nominatim policy.

Usage:
    python3 enrich_zip_codes.py [--dry-run] [--limit 100]
"""

import argparse
import os
import json
import logging
import time
import urllib.request
from typing import Optional

import psycopg2
import psycopg2.extras

from dotenv import load_dotenv
load_dotenv()

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

log = logging.getLogger(__name__)


def reverse_geocode_zip(lat: float, lng: float) -> Optional[str]:
    """Reverse geocode lat/lng to ZIP code using Nominatim."""
    url = f"{NOMINATIM_REVERSE}?lat={lat}&lon={lng}&format=json&zoom=15"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PropToIt/1.0 (https://proptoit.com)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            address = data.get("address", {})
            postcode = address.get("postcode")
            if postcode:
                return str(postcode)
    except Exception as exc:
        log.warning("Nominatim reverse failed for (%s, %s): %s", lat, lng, exc)
    return None


def upsert_zip_code(conn, parcel_id: str, postal_code: str, dry_run: bool = False) -> bool:
    """Update postal_code for a property."""
    if dry_run:
        return True
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE properties SET postal_code = %s, updated_at = NOW()
            WHERE parcel_id = %s AND postal_code = '37401'
        """, (postal_code, parcel_id))
        conn.commit()
        return cur.rowcount > 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max properties to process")
    args = parser.parse_args()

    conn = psycopg2.connect(db_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT parcel_id, lat, lng, address
                FROM properties
                WHERE lat IS NOT NULL AND lng IS NOT NULL AND postal_code = '37401'
                ORDER BY lat, lng
            """)
            props = cur.fetchall()
            if args.limit:
                props = props[:args.limit]

        log.info("Processing %d properties for ZIP code enrichment", len(props))

        updated = 0
        skipped = 0
        failed = 0

        for i, prop in enumerate(props):
            parcel_id = prop["parcel_id"]
            lat = prop["lat"]
            lng = prop["lng"]

            # Rate limit: 1 req/sec
            if i > 0:
                time.sleep(1.0)

            zip_code = reverse_geocode_zip(lat, lng)

            if zip_code:
                if upsert_zip_code(conn, parcel_id, zip_code, args.dry_run):
                    updated += 1
                    log.info("[%d/%d] %s → %s", i+1, len(props), parcel_id, zip_code)
                else:
                    skipped += 1
                    log.debug("[%d/%d] %s already has real ZIP", i+1, len(props), parcel_id)
            else:
                failed += 1
                log.warning("[%d/%d] %s: no ZIP from reverse geocode", i+1, len(props), parcel_id)

        log.info(
            "Done: %d updated, %d skipped, %d failed, %d total",
            updated, skipped, failed, len(props)
        )
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
