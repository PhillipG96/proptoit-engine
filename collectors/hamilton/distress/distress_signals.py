#!/usr/bin/env python3
"""
Distress Signal Collector — Hamilton County, TN

Derives distress signals from existing property data:
- Code violations: Old buildings with low values (proxy for code issues)
- Vacancy: Long-held low-value properties (proxy for vacant)
- Tax delinquent: Real data from county PDF (see tax_delinquent.py)
- Probate: Properties not sold in 30+ years (owner likely deceased)

Usage:
  python3 distress_signals.py [--signal code_violation|vacancy|tax_delinquent|probate|all]
  python3 distress_signals.py --dry-run
"""

import json
import os
import sys
import argparse
import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/magicofelipe/proptoit")
from collectors.base import normalize_record, get_scraped_at

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


def fetch_code_violations(conn):
    """Derive code violation signals from property characteristics.
    
    Properties likely to have code violations:
    - Built before 1950 (old construction standards)
    - Low assessed value (neglected properties)
    - Small buildings (likely residential, not commercial)
    """
    records = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT p.id, p.parcel_id, p.address, p.year_built, p.assessed_value, p.building_sqft
            FROM properties p
            WHERE p.county_id = 'hamilton'
            AND p.year_built < 1950
            AND p.building_sqft > 0
            AND p.assessed_value < 50000
            AND p.assessed_value > 0
        """)
        for prop in cur.fetchall():
            record = normalize_record(
                parcel_id=prop["parcel_id"],
                address=prop["address"],
                lat=35.0456,
                lng=-85.3097,
                source="code_violation_derived",
                raw_data={
                    "year_built": str(prop["year_built"]),
                    "assessed_value": str(prop["assessed_value"]),
                    "building_sqft": str(prop["building_sqft"]),
                },
                distress_signals=["code_violation"]
            )
            records.append(record)
    return records


def fetch_vacancy(conn):
    """Derive vacancy signals using multi-signal scoring model.

    Delegates to the standalone vacancy detector (vacancy.py) which uses
    a weighted scoring model across multiple signals:
    - Tax delinquent (+3), Low assessed value (+2), Not sold 15+ years (+2),
    - No owner mailing address (+1), Code violation (+1), Year built < 1940 (+1)
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    from collectors.hamilton.distress.vacancy import score_properties
    return score_properties(conn)


def fetch_probate(conn):
    """Derive probate signals from long-held properties.
    
    Properties not sold in 30+ years likely have deceased owners:
    - No recent sale activity
    - Owner may have passed away without transferring title
    """
    records = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT p.id, p.parcel_id, p.address, p.last_sale_date
            FROM properties p
            WHERE p.county_id = 'hamilton'
            AND p.last_sale_date IS NOT NULL
            AND p.last_sale_date < NOW() - INTERVAL '30 years'
        """)
        for prop in cur.fetchall():
            record = normalize_record(
                parcel_id=prop["parcel_id"],
                address=prop["address"],
                lat=35.0456,
                lng=-85.3097,
                source="probate_derived",
                raw_data={
                    "last_sale_date": str(prop["last_sale_date"]),
                },
                distress_signals=["probate"]
            )
            records.append(record)
    return records


SIGNAL_COLLECTORS = {
    "code_violation": fetch_code_violations,
    "vacancy": fetch_vacancy,
    "probate": fetch_probate,
}


def upsert_signals(conn, records, signal_type, dry_run=False):
    """Insert signals into distress_signals table."""
    count = 0
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM distress_signals WHERE signal_type = '{signal_type}'")
        conn.commit()

    for record in records:
        metadata = json.dumps(record.get("raw_data", {}))
        # Use score-based confidence for vacancy signals, default 0.6 for others
        confidence = record.get("_confidence", 0.6)
        if dry_run:
            print(f"  [DRY] {record['parcel_id']}: {record['address']}")
        else:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO distress_signals (property_id, signal_type, detected_at, source, severity, confidence, metadata)
                    VALUES (
                        (SELECT id FROM properties WHERE parcel_id = %(parcel_id)s AND county_id = 'hamilton' LIMIT 1),
                        %(signal_type)s, NOW(), %(source)s, 'medium', %(confidence)s, %(metadata)s::jsonb
                    )
                    ON CONFLICT DO NOTHING
                """, {
                    "parcel_id": record["parcel_id"],
                    "signal_type": signal_type,
                    "source": record["source"],
                    "confidence": confidence,
                    "metadata": metadata,
                })
            conn.commit()
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Distress Signal Collector")
    parser.add_argument("--signal", choices=["code_violation", "vacancy", "tax_delinquent", "probate", "all"],
                        default="all", help="Signal type to collect")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("DISTRESS SIGNAL COLLECTOR — Hamilton County, TN")
    print("=" * 60)
    print("\nDISABLED: All heuristic-derived signal collectors were removed"
          "\nunder the no-heuristic policy (verified real data only)."
          "\nReal sources:"
          "\n  - tax_delinquent: collectors/hamilton/distress/tax_delinquent.py"
          "\n                    + scripts/parse_ctrudelq.py (county bulk file)"
          "\n  - probate:        collectors/hamilton/distress/probate_scraper.py (TFP notices)"
          "\nNo verified source exists for code_violation, vacancy, or fsbo yet.")
    return

    conn = psycopg2.connect(db_dsn())
    try:
        signals_to_collect = ["code_violation", "vacancy", "tax_delinquent", "probate"]
        if args.signal != "all":
            signals_to_collect = [args.signal]

        for signal_type in signals_to_collect:
            print(f"\nCollecting {signal_type}...")
            collector = SIGNAL_COLLECTORS[signal_type]
            records = collector(conn)
            print(f"  Found {len(records)} records")

            if records:
                n = upsert_signals(conn, records, signal_type, dry_run=args.dry_run)
                print(f"  Wrote {n} signals")
            else:
                print("  No records found")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
