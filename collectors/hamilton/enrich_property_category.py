#!/usr/bin/env python3
"""
Enrich properties with CURRENTUSE from Hamilton County GIS API,
then classify each into property_category.

Uses requests + subprocess (psql) to avoid DB driver issues.

Classification:
  RS, 0CR  -> residential
  CO, IN, MF, 0EC  -> commercial
  EX, 0ER  -> exempt
  AG       -> agricultural
  NULL/empty -> unknown

Usage:
  python3 enrich_property_category.py [--dry-run] [--limit N] [--delay 0.3]
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from collectors.base import get_county_config
from proptoit.adapters.hamilton import get_extras

import requests  # noqa: E402  (used in query_gis_current_use)

# Hamilton County specifics come from the committed adapter YAML
# (proptoit/adapters/hamilton/hamilton.yaml) — edit there, not here.
_CFG = get_county_config()
GIS_URL = _CFG.gis_endpoints["parcels_query"]
CATEGORY_MAP = get_extras()["category_map"]

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

log = logging.getLogger(__name__)


def psql_t(query: str) -> str:
    """Run psql and return text output."""
    result = subprocess.run(
        ["psql", db_dsn(), "-t", "-c", query],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Raise, don't just log: callers count updates by absence of
        # exceptions, so a logged-only failure lied about writes.
        raise RuntimeError(f"psql failed: {result.stderr[:200]}")
    return result.stdout.strip()


def get_unclassified(limit: int | None = None) -> list[str]:
    q = "SELECT parcel_id FROM properties WHERE current_use IS NULL ORDER BY id"
    if limit:
        q += f" LIMIT {limit}"
    output = psql_t(q)
    return [line.strip() for line in output.split("\n") if line.strip()]


def sql_lit(value: str) -> str:
    """Escape a string for single-quoted SQL literal context (doubling quotes)."""
    return str(value).replace("'", "''")


def update_property(parcel_id: str, current_use: str, category: str):
    sql = (f"UPDATE properties SET current_use = '{sql_lit(current_use)}', "
           f"property_category = '{sql_lit(category)}', updated_at = now() "
           f"WHERE parcel_id = '{sql_lit(parcel_id)}'")
    psql_t(sql)


def query_gis_current_use(tax_map_no: str) -> str | None:
    try:
        r = requests.get(GIS_URL, params={
            "where": f"TAX_MAP_NO = '{tax_map_no}'",
            "outFields": "CURRENTUSE",
            "returnGeometry": "false",
            "f": "json"
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("features"):
            return data["features"][0]["attributes"].get("CURRENTUSE")
        return None
    except Exception as e:
        log.debug("GIS error for %s: %s", tax_map_no, e)
        return None


def classify(current_use: str) -> str:
    if not current_use:
        return "unknown"
    return CATEGORY_MAP.get(str(current_use).strip(), "unknown")


def main():
    parser = argparse.ArgumentParser(description="Enrich properties with GIS land-use data")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")

    parcels = get_unclassified(args.limit)
    total = len(parcels)
    log.info("Found %d unclassified parcels", total)

    updated = 0
    no_data = 0
    errors = 0

    for i, parcel_id in enumerate(parcels):
        # Try raw parcel_id first (preserves double spaces), then normalized
        current_use = query_gis_current_use(parcel_id)
        if current_use is None:
            normalized = ' '.join(parcel_id.split())
            if normalized != parcel_id:
                current_use = query_gis_current_use(normalized)

        if not current_use:
            no_data += 1
            continue

        category = classify(current_use)

        if args.dry_run:
            print(f"  [{i+1}/{total}] {parcel_id} -> {current_use} ({category})")
        else:
            update_property(parcel_id, str(current_use), category)
            log.info("  [%d/%d] %s -> %s (%s)", i+1, total, parcel_id, current_use, category)

        updated += 1
        time.sleep(args.delay)

        if (i + 1) % 50 == 0:
            log.info("Progress: %d/%d (updated=%d, no_data=%d)", i+1, total, updated, no_data)

    # Print summary
    summary = psql_t("""
        SELECT property_category, COUNT(*) FROM properties 
        WHERE property_category != 'unknown'
        GROUP BY property_category ORDER BY COUNT(*) DESC;
    """)
    log.info("\nFinal distribution:\n%s", summary)
    log.info("Done: %d updated, %d no GIS data (dry_run=%s)", updated, no_data, args.dry_run)


if __name__ == "__main__":
    main()
