#!/usr/bin/env python3
"""
FSBO Collector — Chattanooga, TN (Craigslist real estate)

Extracts FSBO listings from Craigslist JSON-LD structured data.
Matches listings to existing parcels in the properties table by address.

Outputs: fsbo distress signals in distress_signals table.

Usage:
  python3 fsbo_craigslist.py [--dry-run]
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request

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
CL_URL = "https://chattanooga.craigslist.org/search/rea"


def fetch_craigslist():
    """Fetch Craigslist Chattanooga real estate page."""
    req = Request(CL_URL, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_jsonld(html):
    """Extract listing data from JSON-LD script tags."""
    listings = []
    pattern = re.compile(
        r'<script[^>]*id="ld_searchpage_results"[^>]*>\s*(.*?)\s*</script>',
        re.DOTALL
    )
    m = pattern.search(html)
    if not m:
        return listings

    try:
        data = json.loads(m.group(1))
        items = data.get("itemListElement", [])
        for item in items:
            listing = item.get("item", {})
            name = listing.get("name", "")
            addr = listing.get("address", {})
            locality = addr.get("addressLocality", "")
            region = addr.get("addressRegion", "")
            lat = listing.get("latitude")
            lng = listing.get("longitude")
            price_match = re.search(r"\$([\d,]+)", name)
            price = price_match.group(1) if price_match else None

            if region not in ("TN", "GA", ""):
                continue

            listings.append({
                "title": name,
                "locality": locality or "Chattanooga",
                "lat": lat,
                "lng": lng,
                "price": price,
                "source_url": CL_URL,
            })
    except (json.JSONDecodeError, KeyError) as e:
        print(f"JSON-LD parse error: {e}")

    return listings


def find_matching_parcels(conn, listings):
    """Match FSBO listings to existing parcels.
    
    Two-tier matching:
    1. Geospatial: Match by PostGIS coordinates (if available)
    2. Address fuzzy: Match by title keywords against property addresses
    """
    matched = []

    # Try geospatial matching first
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, parcel_id, address, lng, lat
            FROM properties WHERE county_id = 'hamilton' AND lat IS NOT NULL AND lng IS NOT NULL
        """)
        properties = cur.fetchall()

    geo_matched_ids = set()
    for listing in listings:
        if not listing.get("lat") or not listing.get("lng"):
            continue
        best_match = None
        best_dist = 0.05  # ~3 mile threshold

        for prop in properties:
            if not prop.get("lat") or not prop.get("lng"):
                continue
            dist = ((listing["lat"] - prop["lat"])**2 + (listing["lng"] - prop["lng"])**2)**0.5
            if dist < best_dist:
                best_dist = dist
                best_match = prop

        if best_match:
            matched.append({
                "property_id": best_match["id"],
                "listing": listing,
                "distance": best_dist,
            })
            geo_matched_ids.add(best_match["id"])

    # Fallback: address keyword matching for listings without geo match
    if properties:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if geo_matched_ids:
                cur.execute("""
                    SELECT id, parcel_id, address FROM properties
                    WHERE county_id = 'hamilton' AND id <> ALL(%s)
                """, (list(geo_matched_ids),))
            else:
                cur.execute("""
                    SELECT id, parcel_id, address FROM properties
                    WHERE county_id = 'hamilton'
                """)
            all_props = cur.fetchall()

        for listing in listings:
            title = listing.get("title", "").lower()
            for prop in all_props:
                addr = (prop.get("address") or "").lower()
                if any(part in addr for part in _extract_addr_parts(title)):
                    matched.append({
                        "property_id": prop["id"],
                        "listing": listing,
                        "distance": None,  # fuzzy match
                    })
                    break

    return matched


def _extract_addr_parts(title):
    """Extract address-like components from a listing title."""
    import re
    parts = []
    nums = re.findall(r'\b(\d{2,5})\b', title)
    parts.extend(nums)
    streets = re.findall(r'\b([A-Za-z]{2,}\s+(?:st|ave|dr|blvd|ln|ct|rd|pl|way|cir|ter|pkwy))\b', title, re.I)
    parts.extend(streets)
    return parts


INSERT_SIGNAL = """
    INSERT INTO distress_signals (property_id, signal_type, detected_at, source, severity, confidence, metadata)
    VALUES (%(property_id)s, 'fsbo', NOW(), 'craigslist', 'medium', 0.7, %(metadata)s)
    ON CONFLICT DO NOTHING
"""

INSERT_FSBO_LISTING = """
    INSERT INTO fsbo_listings (source, source_url, price, title, latitude, longitude, scraped_at, county_id)
    VALUES (%(source)s, %(source_url)s, %(price)s, %(title)s, %(latitude)s, %(longitude)s, NOW(), 'hamilton')
    RETURNING id
"""


def upsert_fsbo_listings(conn, listings, dry_run=False):
    """Write ALL Craigslist listings to fsbo_listings table.

    The DELETE+INSERT runs in ONE transaction so a mid-run failure can't
    leave the table wiped-and-unrefilled. An empty scrape result is
    treated as a scrape failure and refuses the wipe — previously a page
    layout change returned [] and the refresh deleted every row.
    """
    if not listings:
        print("upsert_fsbo_listings: 0 listings passed — refusing to wipe existing rows")
        return 0

    if dry_run:
        for listing in listings:
            print(f"  [DRY] {(listing.get('title') or '')[:50]}")
        return 0

    count = 0
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fsbo_listings WHERE source = 'craigslist'")
            for listing in listings:
                price_str = listing.get("price")
                price = None
                if price_str:
                    try:
                        price = float(price_str.replace(",", ""))
                    except ValueError:
                        pass

                row = {
                    "source": "craigslist",
                    "source_url": listing.get("source_url", CL_URL),
                    "price": price,
                    "title": listing.get("title", ""),
                    "latitude": listing.get("lat"),
                    "longitude": listing.get("lng"),
                }
                cur.execute(INSERT_FSBO_LISTING, row)
                count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return count


def main():
    print("=" * 60)
    print("FSBO COLLECTOR — Craigslist Chattanooga")
    print("=" * 60)

    dry_run = "--dry-run" in sys.argv

    html = fetch_craigslist()
    listings = extract_jsonld(html)
    print(f"Fetched {len(listings)} listings from Craigslist")

    conn = psycopg2.connect(db_dsn())
    try:
        matched = find_matching_parcels(conn, listings)
        print(f"Matched {len(matched)} listings to existing parcels")

        # Write ALL listings to fsbo_listings table
        n_listings = upsert_fsbo_listings(conn, listings, dry_run=dry_run)
        print(f"Wrote {n_listings} FSBO listings to fsbo_listings table")

        # Signal writing DISABLED (no-heuristic policy): the address matcher is fuzzy
        # (match_distance) and produced unverifiable signals. Listings are still
        # collected for future matching once a geocoder-based pipeline exists.
    finally:
        conn.close()


if __name__ == "__main__":
    main()
