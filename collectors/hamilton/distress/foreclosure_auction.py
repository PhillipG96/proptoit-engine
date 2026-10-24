#!/usr/bin/env python3
"""
Foreclosure Collector — Hamilton County, TN

Derives pre-foreclosure signals from multiple sources:
1. External legal notices (Daily Report, etc.)
2. Tax delinquency patterns (high tax ratios indicate foreclosure risk)
3. Properties with multiple distress signals

Outputs: pre_foreclosure distress signals

Usage:
  python3 foreclosure_auction.py [--dry-run]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/magicofelipe/proptoit")
from collectors.base import normalize_record, save_cache, load_cache, get_scraped_at

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
CACHE_PATH = "/tmp/proptoit/foreclosure_auction.cache"
OUTPUT_PATH = "/tmp/proptoit/foreclosure_auction.json"

# External sources (best-effort, many are DNS-blocked from this server)
SOURCES = [
    {
        "name": "Daily Report (TN legal notices)",
        "url": "https://www.dailyreportonline.com/legal-notices",
        "search_terms": ["foreclosure", "sheriff sale", "trustee sale"],
    },
]


def fetch_external_sources():
    """Fetch foreclosure listings from external sources."""
    results = []
    for source in SOURCES:
        try:
            req = Request(source["url"], headers={"User-Agent": "Proptoit-Collector/1.0"})
            resp = urlopen(req, timeout=10)
            html = resp.read(50000).decode("utf-8", errors="replace")
            html_lower = html.lower()

            if not any(term in html_lower for term in source["search_terms"]):
                continue

            # Extract parcel IDs
            parcel_pattern = re.compile(r'(\d{3,5}-\d{1,3}-\d{3,5})')
            parcels = parcel_pattern.findall(html)

            for pid in parcels:
                record = normalize_record(
                    parcel_id=pid.strip(),
                    address="Hamilton County, TN",
                    lat=35.0456,
                    lng=-85.3097,
                    source="foreclosure_auction",
                    raw_data={"source_site": source["name"]},
                    distress_signals=["pre_foreclosure"]
                )
                results.append(record)
        except Exception as e:
            print(f"  External source {source['name']}: {e}")

    return results


def fetch_derived_signals(conn):
    """Derive pre-foreclosure signals from existing data.
    
    Properties likely in foreclosure:
    - High tax ratio (>2% of assessed value)
    - Already have tax_delinquent + another distress signal
    - Multiple distress signals (compounding risk)
    """
    records = []

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # High tax ratio properties (likely in foreclosure process)
        cur.execute("""
            SELECT p.id, p.parcel_id, p.address, p.assessed_value, p.tax_amount
            FROM properties p
            WHERE p.county_id = 'hamilton'
            AND p.tax_amount > 0 AND p.assessed_value > 0
            AND (p.tax_amount::numeric / p.assessed_value::numeric) > 0.02
        """)
        for prop in cur.fetchall():
            record = normalize_record(
                parcel_id=prop["parcel_id"],
                address=prop["address"],
                lat=35.0456,
                lng=-85.3097,
                source="foreclosure_derived",
                raw_data={
                    "assessed_value": str(prop["assessed_value"]),
                    "tax_amount": str(prop["tax_amount"]),
                    "tax_ratio": str(float(prop["tax_amount"]) / float(prop["assessed_value"])) if prop["assessed_value"] else "0"
                },
                distress_signals=["pre_foreclosure"]
            )
            records.append(record)

        # Properties with multiple distress signals (compounding risk)
        cur.execute("""
            SELECT p.id, p.parcel_id, p.address, COUNT(*) as signal_count
            FROM distress_signals ds
            JOIN properties p ON p.id = ds.property_id
            WHERE p.county_id = 'hamilton' AND ds.signal_type != 'pre_foreclosure'
            GROUP BY p.id, p.parcel_id, p.address
            HAVING COUNT(*) >= 2
        """)
        for prop in cur.fetchall():
            record = normalize_record(
                parcel_id=prop["parcel_id"],
                address=prop["address"],
                lat=35.0456,
                lng=-85.3097,
                source="foreclosure_derived_multi_signal",
                raw_data={"signal_count": str(prop["signal_count"])},
                distress_signals=["pre_foreclosure"]
            )
            records.append(record)

    return records


def upsert_foreclosure_signals(conn, records, dry_run=False):
    """Insert pre_foreclosure signals into distress_signals table."""
    count = 0

    # Remove old derived foreclosure signals
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM distress_signals WHERE signal_type = 'pre_foreclosure' AND source LIKE 'foreclosure_derived%'")
        conn.commit()

    for record in records:
        metadata = json.dumps(record.get("raw_data", {}))
        if dry_run:
            print(f"  [DRY] {record['parcel_id']}: {record['address']}")
        else:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO distress_signals (property_id, signal_type, detected_at, source, severity, confidence, metadata)
                    VALUES (
                        (SELECT id FROM properties WHERE parcel_id = %(parcel_id)s AND county_id = 'hamilton' LIMIT 1),
                        'pre_foreclosure', NOW(), %(source)s, 'high', 0.8, %(metadata)s
                    )
                    ON CONFLICT DO NOTHING
                """, {
                    "parcel_id": record["parcel_id"],
                    "source": record["source"],
                    "metadata": metadata,
                })
            conn.commit()
            count += 1

    return count


def main():
    print("=" * 60)
    print("FORECLOSURE COLLECTOR — Hamilton County, TN")
    print("=" * 60)
    print("\nDISABLED: heuristic-derived pre_foreclosure signals removed under"
          "\nthe no-heuristic policy. fetch_derived_signals() fabricated signals"
          "\nfrom tax-ratio and multi-signal proxies. No verified foreclosure"
          "\nsource is wired up yet — candidate: foreclosure sale notices on"
          "\nthe TFP legal-notices page (see probate_scraper.py fetch pattern).")
    return

    dry_run = "--dry-run" in sys.argv

    # Fetch from external sources
    print("\nFetching external sources...")
    external_records = fetch_external_sources()
    print(f"External records: {len(external_records)}")

    # Derive from existing data
    print("\nDeriving from existing data...")
    conn = psycopg2.connect(db_dsn())
    try:
        derived_records = fetch_derived_signals(conn)
        print(f"Derived records: {len(derived_records)}")

        # Combine all records
        all_records = external_records + derived_records
        print(f"\nTotal foreclosure records: {len(all_records)}")

        # Write signals
        if all_records:
            n = upsert_foreclosure_signals(conn, all_records, dry_run=dry_run)
            print(f"Wrote {n} pre_foreclosure signals")
        else:
            print("No foreclosure records found")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
