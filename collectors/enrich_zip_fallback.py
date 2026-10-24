#!/usr/bin/env python3
"""
Enrich ZIP codes using Census lat/lng → ZIP lookup.
Uses Census Geocoding API reverse lookup as alternative to Nominatim.
"""
import os
import json
import time
import urllib.request
import psycopg2

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

# Use Geoapify reverse geocoding (free tier: 30,000 requests/day, no key needed for basic)
# Or use US Census ZIP lookup by lat/lng
# Alternative: batch lookup via US Zip Code API

# Actually let's try the Nominatim reverse with proper delay and fallback
# Also try the US Census ZIP Code Tabulation Area lookup


def get_missing():
    """Get properties with coords but no real ZIP."""
    conn = psycopg2.connect(db_dsn())
    cur = conn.cursor()
    cur.execute("""
        SELECT id, parcel_id, lat, lng, address
        FROM properties
        WHERE lat IS NOT NULL AND (postal_code = '37401' OR postal_code IS NULL)
        ORDER BY lat, lng
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def reverse_geocode_zip(lat, lng):
    """Try Nominatim reverse geocoding with retries."""
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json&zoom=15"
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "PropToIt/1.0 (contact@proptoit.com)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                address = data.get("address", {})
                postcode = address.get("postcode")
                if postcode:
                    return str(postcode)
            return None
        except Exception as e:
            if "429" in str(e):
                time.sleep(5 * (attempt + 1))
            else:
                return None
    return None


def update_zips(results):
    """Bulk update ZIPs in DB."""
    conn = psycopg2.connect(db_dsn())
    cur = conn.cursor()
    for db_id, postal_code in results:
        cur.execute("""
            UPDATE properties SET postal_code = %s, updated_at = NOW()
            WHERE id = %s AND postal_code = '37401'
        """, (postal_code, db_id))
    conn.commit()
    updated = cur.rowcount
    cur.close()
    conn.close()
    return updated


def main():
    rows = get_missing()
    print(f"Found {len(rows)} properties needing ZIP enrichment", flush=True)
    if not rows:
        print("All done.")
        return

    results = []
    skipped = 0
    failed = 0

    for i, (db_id, parcel_id, lat, lng, address) in enumerate(rows):
        time.sleep(1.5)  # Be respectful to Nominatim
        zipcode = reverse_geocode_zip(lat, lng)
        if zipcode:
            results.append((db_id, zipcode))
            print(f"[{i+1}/{len(rows)}] {parcel_id} → {zipcode}", flush=True)
        else:
            failed += 1
            print(f"[{i+1}/{len(rows)}] {parcel_id}: no ZIP", flush=True)

    if results:
        updated = update_zips(results)
        print(f"\nUpdated {updated} properties with real ZIPs", flush=True)

    # Final count
    conn = psycopg2.connect(db_dsn())
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            count(*), 
            count(CASE WHEN lat IS NOT NULL THEN 1 END),
            count(CASE WHEN postal_code != '37401' AND postal_code IS NOT NULL THEN 1 END)
        FROM properties
    """)
    total, with_lat, with_zip = cur.fetchone()
    cur.close()
    conn.close()
    print(f"DB: {with_lat}/{total} coords, {with_zip}/{total} real ZIPs", flush=True)


if __name__ == "__main__":
    main()
