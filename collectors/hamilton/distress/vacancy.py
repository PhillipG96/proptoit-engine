#!/usr/bin/env python3
"""
Vacancy Signal Detector — Hamilton County, TN

Multi-signal heuristic since no public vacancy data exists:
- Tax delinquent (+3 points) — delinquent properties more likely vacant
- Low assessed value (+2 points) — neglected properties
- Not sold in 15+ years (+2 points) — long-held properties
- No owner mailing address (+1 point) — owner disconnected
- Code violation signal (+1 point) — neglected properties
- Year built < 1940 (+1 point) — older construction, more likely neglected

Threshold: score >= 5 → vacancy signal (calibrated for Hamilton County data)
Confidence: 0.5 (score 5), 0.65 (score 6), 0.75 (score 7), 0.85 (score 8+)
Source: "vacancy_multi_signal"
"""

import os
import json
import sys
import argparse
import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/magicofelipe/proptoit")
from collectors.base import normalize_record

from dotenv import load_dotenv
load_dotenv()

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")

# Scoring thresholds
LOW_VALUE_THRESHOLD = 30000  # assessed_value < 30K → +2
NOT_SOLD_YEARS = 15  # not sold in 15+ years → +2
OLD_BUILD_YEAR = 1940  # year_built < 1940 → +1
SCORE_THRESHOLD = 5  # minimum score to flag as vacancy


def get_confidence(score):
    """Map score to confidence level."""
    if score >= 8:
        return 0.85
    elif score >= 7:
        return 0.75
    elif score >= 6:
        return 0.65
    else:
        return 0.5


def score_properties(conn):
    """Score all Hamilton County properties using multi-signal vacancy model.

    Returns list of dicts with property info, score breakdown, and confidence.
    Only returns properties meeting the score threshold.
    """
    results = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                p.id, p.parcel_id, p.address, p.assessed_value,
                p.last_sale_date, p.owner_mailing_address,
                p.year_built, p.owner_occupant, p.building_sqft,
                p.lat, p.lng,
                -- Tax delinquent signal (+3)
                CASE WHEN EXISTS (
                    SELECT 1 FROM distress_signals ds
                    WHERE ds.property_id = p.id AND ds.signal_type = 'tax_delinquent'
                ) THEN 3 ELSE 0 END AS tax_delinquent,
                -- Low assessed value (+2)
                CASE WHEN p.assessed_value > 0 AND p.assessed_value < %(low_value)s THEN 2 ELSE 0 END AS low_value,
                -- Not sold in 15+ years (+2)
                CASE WHEN p.last_sale_date IS NOT NULL
                    AND p.last_sale_date < NOW() - INTERVAL '%(not_sold)s years'
                THEN 2 ELSE 0 END AS not_sold,
                -- No owner mailing address (+1)
                CASE WHEN p.owner_mailing_address IS NULL
                    OR p.owner_mailing_address = ''
                THEN 1 ELSE 0 END AS no_address,
                -- Code violation signal (+1)
                CASE WHEN EXISTS (
                    SELECT 1 FROM distress_signals ds
                    WHERE ds.property_id = p.id AND ds.signal_type = 'code_violation'
                ) THEN 1 ELSE 0 END AS code_violation,
                -- Year built < 1940 (+1)
                CASE WHEN p.year_built IS NOT NULL AND p.year_built < %(old_build)s
                THEN 1 ELSE 0 END AS old_build
            FROM properties p
            WHERE p.county_id = 'hamilton'
        """, {
            "low_value": LOW_VALUE_THRESHOLD,
            "not_sold": NOT_SOLD_YEARS,
            "old_build": OLD_BUILD_YEAR,
        })

        for prop in cur.fetchall():
            score = (
                prop["tax_delinquent"]
                + prop["low_value"]
                + prop["not_sold"]
                + prop["no_address"]
                + prop["code_violation"]
                + prop["old_build"]
            )

            if score < SCORE_THRESHOLD:
                continue

            confidence = get_confidence(score)
            score_breakdown = {
                "tax_delinquent": prop["tax_delinquent"],
                "low_assessed_value": prop["low_value"],
                "not_sold_15yrs": prop["not_sold"],
                "no_owner_mailing_address": prop["no_address"],
                "code_violation": prop["code_violation"],
                "year_built_pre_1940": prop["old_build"],
                "total_score": score,
            }

            record = normalize_record(
                parcel_id=prop["parcel_id"],
                address=prop["address"],
                lat=prop["lat"] or 35.0456,
                lng=prop["lng"] or -85.3097,
                source="vacancy_multi_signal",
                raw_data={
                    "score_breakdown": score_breakdown,
                    "assessed_value": str(prop["assessed_value"]) if prop["assessed_value"] else "null",
                    "last_sale_date": str(prop["last_sale_date"]) if prop["last_sale_date"] else "null",
                    "year_built": str(prop["year_built"]) if prop["year_built"] else "null",
                },
                distress_signals=["vacancy"]
            )
            record["_score"] = score
            record["_confidence"] = confidence
            results.append(record)

    return results


def write_signals(conn, records, dry_run=False):
    """Write vacancy signals to distress_signals table.

    Deletes old vacancy signals first, then inserts new ones with
    score-based confidence and metadata containing the score breakdown.
    """
    count = 0

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM distress_signals WHERE signal_type = 'vacancy'")
        conn.commit()

    for record in records:
        score = record["_score"]
        confidence = record["_confidence"]
        metadata = json.dumps(record.get("raw_data", {}))

        if dry_run:
            print(f"  [DRY] {record['parcel_id']}: {record['address']} "
                  f"(score={score}, confidence={confidence})")
        else:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO distress_signals
                        (property_id, signal_type, detected_at, source, severity, confidence, metadata)
                    VALUES (
                        (SELECT id FROM properties WHERE parcel_id = %(parcel_id)s
                         AND county_id = 'hamilton' LIMIT 1),
                        %(signal_type)s, NOW(), %(source)s,
                        %(severity)s, %(confidence)s, %(metadata)s::jsonb
                    )
                    ON CONFLICT DO NOTHING
                """, {
                    "parcel_id": record["parcel_id"],
                    "signal_type": "vacancy",
                    "source": "vacancy_multi_signal",
                    "severity": "medium",
                    "confidence": confidence,
                    "metadata": metadata,
                })
            conn.commit()
            count += 1

    return count


def main():
    print("DISABLED: vacancy.py is a multi-signal heuristic detector (weighted scoring"
          "\nacross tax delinquency, assessed value, sale age, etc.) — removed under"
          "\nthe no-heuristic policy. No verified vacancy source exists yet.")
    return
    parser = argparse.ArgumentParser(
        description="Multi-signal Vacancy Signal Detector — Hamilton County, TN"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview results without writing to database")
    args = parser.parse_args()

    print("=" * 60)
    print("VACANCY SIGNAL DETECTOR — Hamilton County, TN")
    print("=" * 60)
    print(f"  Score threshold: >= {SCORE_THRESHOLD}")
    print(f"  Low value threshold: < ${LOW_VALUE_THRESHOLD:,}")
    print(f"  Not sold threshold: {NOT_SOLD_YEARS}+ years")
    print(f"  Old build threshold: < {OLD_BUILD_YEAR}")
    print()

    conn = psycopg2.connect(DB_DSN)
    try:
        records = score_properties(conn)
        print(f"  Found {len(records)} properties with score >= {SCORE_THRESHOLD}")

        if records:
            # Show score distribution
            score_dist = {}
            for r in records:
                s = r["_score"]
                score_dist[s] = score_dist.get(s, 0) + 1
            print("  Score distribution:")
            for s in sorted(score_dist.keys()):
                conf = get_confidence(s)
                print(f"    Score {s}: {score_dist[s]} properties (confidence: {conf})")
            print()

            n = write_signals(conn, records, dry_run=args.dry_run)
            if args.dry_run:
                print(f"\n  [DRY RUN] Would write {n} vacancy signals")
            else:
                print(f"  Wrote {n} vacancy signals to distress_signals table")
        else:
            print("  No properties met the vacancy threshold")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
