#!/usr/bin/env python3
"""Harris County Distress Signals Collector

Derives and fetches distress signals for Harris County properties.
Sources:
- Estate/Trust ownership (probate)
- Absentee ownership (LLC/Corp)
- Old small properties (deterioration)
- FSBO (Craigslist Houston)

Usage:
  python3 harris_distress.py --dry-run [--force-refresh]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request

import psycopg2
import psycopg2.extras
from psycopg2 import sql

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
OUTPUT_PATH = "/tmp/proptoit/harris_distress.json"
CL_URL = "https://houston.craigslist.org/search/rea"


def get_db_conn():
    return psycopg2.connect(db_dsn())


def summarize_harris(db):
    """Get summary stats for Harris County."""
    cur = db.cursor()
    cur.execute("""
        SELECT 
            COUNT(*),
            COUNT(*) FILTER (WHERE property_category = 'residential'),
            COUNT(*) FILTER (WHERE property_category = 'commercial'),
            COUNT(*) FILTER (WHERE is_tax_delinquent = true),
            COUNT(*) FILTER (WHERE pre_foreclosure_flag = true),
            COUNT(*) FILTER (WHERE probate_flag = true),
            COUNT(*) FILTER (WHERE fsbo_flag = true),
            COUNT(*) FILTER (WHERE code_violation_flag = true),
            COUNT(*) FILTER (WHERE is_vacant = true),
            COUNT(*) FILTER (WHERE lat IS NOT NULL),
            COUNT(*) FILTER (WHERE assessed_value > 0),
            COUNT(*) FILTER (WHERE last_sale_date IS NOT NULL),
            MIN(assessed_value),
            AVG(assessed_value),
            MAX(assessed_value)
        FROM properties WHERE county_id = 'harris'
    """)
    r = cur.fetchone()
    total, residential, commercial, tax_del, pre_fc, prob, fsbo, code, vacant, geocoded, valued, sold, min_val, avg_val, max_val = r
    cur.close()
    
    print(f"    Total properties: {total:,}")
    print(f"    Residential: {residential:,} ({residential/total*100:.1f}%)")
    print(f"    Commercial: {commercial:,} ({commercial/total*100:.1f}%)")
    print(f"    Tax delinquent: {tax_del:,}")
    print(f"    Pre-foreclosure: {pre_fc:,}")
    print(f"    Probate: {prob:,}")
    print(f"    FSBO: {fsbo:,}")
    print(f"    Code violations: {code:,}")
    print(f"    Vacant: {vacant:,}")
    print(f"    Geocoded (lat): {geocoded:,}")
    print(f"    Valued: {valued:,}")
    print(f"    With sale date: {sold:,}")
    if min_val and avg_val and max_val:
        print(f"    Value range: ${float(min_val):,.0f} - ${float(max_val):,.0f} (avg: ${float(avg_val):,.0f})")


def derive_distress_from_hcad(db, dry_run=False, limit=2000):
    """
    Derive distress signals from HCAD property characteristics.
    
    For Harris County, sale data is largely unavailable, so we rely on:
    - ESTATE ownership (probate)
    - Living trust ownership (probate)
    - Absentee ownership (LLC/Corp/Investment)
    - Old small properties (deterioration risk)
    """
    cur = db.cursor()
    
    # Sample properties with distress-relevant fields
    query = """
        SELECT id, parcel_id, address, city, property_category,
               assessed_value, last_sale_date, last_sale_price,
               owner_of_record, owner_occupant, is_vacant, year_built,
               lot_size_sqft, building_sqft
        FROM properties
        WHERE county_id = 'harris'
          AND property_category = 'residential'
          AND assessed_value > 0
          AND (
              -- Probate signals
              owner_of_record ILIKE '%%estate of%%'
              OR owner_of_record ILIKE '%%living trust%%'
              OR owner_of_record ILIKE '%%family trust%%'
              -- Absentee/investment
              OR owner_of_record ILIKE '%%llc%%'
              OR owner_of_record ILIKE '%%inc%%'
              OR owner_of_record ILIKE '%%corporation%%'
              OR owner_of_record ILIKE '%%investment%%'
              OR owner_of_record ILIKE '%%holdings%%'
              -- Deterioration signal
              OR (year_built IS NOT NULL AND year_built < 1960)
          )
        ORDER BY random()
        LIMIT %d
    """ % limit
    cur.execute(query)
    rows = cur.fetchall()
    
    records = []
    for row in rows:
        (rid, parcel, addr, city, cat, val, sale_date, sale_price,
         owner, owner_occ, vacant, yb, lot_sqft, bldg_sqft) = row
        
        signals = []
        reasons = []
        
        # 1. Estate of (probate)
        if owner and 'estate of' in owner.lower():
            signals.append('probate_flag')
            reasons.append('estate_of_owner')
        
        # 2. Living trust (probate)
        elif owner and ('living trust' in owner.lower() or 'family trust' in owner.lower()):
            signals.append('probate_flag')
            reasons.append('trust_owner')
        
        # 3. Absentee owner (LLC/Corp/Investment)
        elif owner and (
            'llc' in owner.lower() or 'inc' in owner.lower() or
            'corporation' in owner.lower() or 'investment' in owner.lower() or
            'holdings' in owner.lower()
        ):
            signals.append('absentee')
            reasons.append('absentee_owner')
        
        # 4. Old + small (deterioration)
        if yb and yb < 1960 and bldg_sqft and 0 < bldg_sqft < 800:
            signals.append('deteriorating')
            reasons.append('old_small_property')
        
        if signals:
            record = {
                'property_id': rid,
                'parcel_id': parcel,
                'address': addr,
                'city': city,
                'distress_signals': signals,
                'reasons': reasons,
                'assessed_value': float(val) if val else None,
                'last_sale_date': sale_date.isoformat() if sale_date else None,
                'owner': str(owner)[:200] if owner else None,
                'source': 'hcad_derived',
                'scraped_at': datetime.now(timezone.utc).isoformat(),
            }
            records.append(record)
    
    cur.close()
    return records


def fetch_fsbo_craigslist(db, dry_run=False, limit=100):
    """Fetch FSBO listings from Craigslist Houston."""
    try:
        req = Request(CL_URL, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        with urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Craigslist fetch failed: {e}")
        return []
    
    import json as json_mod
    pattern = re.compile(
        r'<script[^>]*id="ld_searchpage_results"[^>]*>\s*(.*?)\s*</script>',
        re.DOTALL
    )
    m = pattern.search(html)
    if not m:
        return []
    
    try:
        data = json_mod.loads(m.group(1))
        items = data.get("itemListElement", [])
    except:
        return []
    
    records = []
    for item in items[:limit]:
        listing = item.get("item", {})
        name = listing.get("name", "")
        price_match = re.search(r"\$([\d,]+)", name)
        price = int(price_match.group(1).replace(',', '')) if price_match else None
        
        if price and price < 1000:
            continue
        
        addr_info = listing.get("address", {})
        locality = addr_info.get("addressLocality", "")
        lat = listing.get("latitude")
        lng = listing.get("longitude")
        
        if lat and lng and 29.0 < lat < 30.2 and -96.0 < lng < -95.0:
            record = {
                'cl_title': name[:200],
                'price': price,
                'location': locality,
                'lat': lat,
                'lng': lng,
                'distress_signals': ['fsbo'],
                'reasons': ['craigslist_houston'],
                'source': 'craigslist',
                'scraped_at': datetime.now(timezone.utc).isoformat(),
            }
            records.append(record)
    
    return records


def write_fsbo_to_db(db, fsbo_records):
    """Match FSBO listings to properties and update fsbo_flag."""
    if not fsbo_records:
        return 0
    
    cur = db.cursor()
    matched = 0
    
    for fsbo in fsbo_records:
        lat = fsbo.get('lat')
        lng = fsbo.get('lng')
        if not lat or not lng:
            continue
        
        cur.execute("""
            SELECT id, parcel_id
            FROM properties
            WHERE county_id = 'harris'
              AND property_category = 'residential'
              AND lat IS NOT NULL
              AND lng IS NOT NULL
            ORDER BY (lat - %s)::float8 * (lat - %s)::float8 +
                     (lng - %s)::float8 * (lng - %s)::float8
            LIMIT 1
        """, (lat, lat, lng, lng))
        nearest = cur.fetchone()
        
        if nearest:
            prop_id, parcel_id = nearest
            cur.execute("""
                UPDATE properties SET fsbo_flag = true, updated_at = NOW()
                WHERE id = %s
            """, (prop_id,))
            matched += 1
    
    db.commit()
    cur.close()
    return matched


def write_derived_distress(db, distress_records):
    """Persist derived distress signals to DB columns."""
    cur = db.cursor()
    updated = 0
    
    for rec in distress_records:
        prop_id = rec['property_id']
        signals = rec.get('distress_signals', [])
        
        # Map signals to DB columns
        updates = {}
        if 'probate_flag' in signals:
            updates['probate_flag'] = True
        # Absentee ownership is useful in reports, but it is not evidence of
        # tax delinquency or vacancy. Keep those stronger flags source-backed.
        if 'deteriorating' in signals:
            updates['code_violation_flag'] = True
        
        if updates:
            # Column names come from the fixed mapping above; wrap them in
            # sql.Identifier so no key ever becomes raw SQL text.
            sets = sql.SQL(', ').join(
                sql.SQL('{} = %s').format(sql.Identifier(k)) for k in updates)
            vals = list(updates.values()) + [prop_id]
            cur.execute(sql.SQL("""
                UPDATE properties 
                SET {}, updated_at = NOW()
                WHERE id = %s
            """).format(sets), vals)
            updated += 1
    
    db.commit()
    cur.close()
    return updated


def run(dry_run=False, force_refresh=False):
    print("=" * 60)
    print("HARRIS COUNTY DISTRESS SIGNALS COLLECTOR")
    print("=" * 60)
    
    conn = get_db_conn()
    
    # Summary
    print("\n>>> Harris County property summary:")
    summarize_harris(conn)
    
    # 1. Derive distress from HCAD indicators (expanded)
    print("\n>>> Deriving distress from HCAD indicators (expanded)...")
    distress_records = derive_distress_from_hcad(conn, dry_run)
    print(f"    Found {len(distress_records)} properties with distress indicators")
    
    # 2. Fetch FSBO listings
    print("\n>>> Fetching FSBO from Craigslist Houston...")
    fsbo_records = fetch_fsbo_craigslist(conn, dry_run)
    print(f"    Found {len(fsbo_records)} FSBO listings in Houston metro")
    
    # 3. Match FSBO to properties
    if fsbo_records and not dry_run:
        matched = write_fsbo_to_db(conn, fsbo_records)
        print(f"    Matched {matched} FSBO listings to Harris parcels")
    
    # 4. Persist derived distress signals to DB
    if distress_records and not dry_run:
        distressed = write_derived_distress(conn, distress_records)
        print(f"    Persisted {distressed} distress signals to DB columns")
    
    # Breakdown by signal type
    if distress_records:
        from collections import Counter
        signal_counts = Counter()
        for rec in distress_records:
            for s in rec.get('distress_signals', []):
                signal_counts[s] += 1
        print(f"\n>>> Signal breakdown:")
        for sig, cnt in signal_counts.most_common():
            print(f"    {sig}: {cnt:,}")
    
    # Summary
    print(f"\n=== SUMMARY ===")
    print(f"  Distress-flagged (derived): {len(distress_records):,}")
    print(f"  FSBO listings: {len(fsbo_records)}")
    
    if dry_run:
        print(f"\n--- DRY RUN — Would write to {OUTPUT_PATH}")
        if distress_records:
            sample = distress_records[0]
            print(f"  Sample distress: {sample['parcel_id']} -> {sample['distress_signals']}")
        if fsbo_records:
            print(f"  Sample FSBO: {fsbo_records[0].get('cl_title', 'N/A')} @ ${fsbo_records[0].get('price')}")
    else:
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        data = {
            'distress_derived': distress_records,
            'fsbo': fsbo_records,
            'scraped_at': datetime.now(timezone.utc).isoformat()
        }
        with open(OUTPUT_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✅ Wrote {OUTPUT_PATH}")
    
    conn.close()
    return {
        'distress': distress_records,
        'fsbo': fsbo_records,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Harris County Distress Signals")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force_refresh=args.force_refresh)
