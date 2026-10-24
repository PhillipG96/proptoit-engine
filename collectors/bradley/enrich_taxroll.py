"""
Bradley County, TN — enrich existing rows with current values + zips from the
TaxParcelQuery FeatureServer (the CAMA/tax-roll publishing layer, fresher than
Parcel_Query which carries the stale 2018 attributes).

Updates:
  postal_code     <- PSTLZIP5
  assessed_value  <- CNTASSDVAL (county assessed value, current roll)
  building_value / land_value / total_value: NOT in this layer (left as-is)
  (kept minimal: only fields this layer is authoritative for)

Match key: parcel_id (PARCELID format matches ours exactly, e.g. '002F A 013.00 000').

Usage:
  python collectors/bradley/enrich_taxroll.py --dry-run
  python collectors/bradley/enrich_taxroll.py
"""
import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit@localhost:5432/proptoit")

LAYER_URL = (
    "https://services1.arcgis.com/Ev0M319ggvf3XvKr/"
    "arcgis/rest/services/TaxParcelQuery/FeatureServer/0/query"
)
PAGE_SIZE = 1000

log = logging.getLogger("bradley.taxroll")

FETCH_FIELDS = "PARCELID,PSTLZIP5,CNTASSDVAL,PRVASSDVAL,PRVTXBLVAL,CNTTXBLVAL,SALEPRICE"


def query_layer(params: dict, retries: int = 4):
    qs = urllib.parse.urlencode(params)
    url = f"{LAYER_URL}?{qs}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=90) as resp:
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as e:  # noqa: BLE001
            last_exc = e
            wait = 2 ** attempt
            log.warning("Query failed (attempt %d/%d): %s — retrying in %ss",
                        attempt + 1, retries, e, wait)
            time.sleep(wait)
    raise last_exc


def fetch_all():
    offset = 0
    total = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": FETCH_FIELDS,
            "returnGeometry": "false",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "json",
        }
        data = query_layer(params)
        feats = data.get("features", [])
        total += len(feats)
        yield feats
        if len(feats) < PAGE_SIZE or not data.get("exceededTransferLimit"):
            break
        offset += PAGE_SIZE
    log.info("Fetched %d features total", total)
    return total  # noqa: RET503


UPDATE_SQL = """
    UPDATE properties SET
        postal_code    = COALESCE(properties.postal_code, %(zip)s),
        assessed_value = %(cnt_assd)s,
        updated_at     = NOW()
    WHERE county_id = 'bradley' AND parcel_id = %(parcel_id)s
      AND (%(cnt_assd)s IS NOT NULL)
"""


def run(dry_run: bool):
    conn = psycopg2.connect(DB_DSN)
    stats = {"fetched": 0, "updated": 0, "unmatched": 0, "zip_filled": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM properties WHERE county_id='bradley'")
            log.info("Existing bradley rows: %d", cur.fetchone()[0])

        batch = []
        for feats in fetch_all():
            for f in feats:
                a = f.get("attributes") or {}
                parcel_id = (a.get("PARCELID") or "").strip()
                if not parcel_id:
                    continue
                stats["fetched"] += 1
                cnt_assd = a.get("CNTASSDVAL")
                if isinstance(cnt_assd, str):
                    cnt_assd = float(cnt_assd) if cnt_assd.strip() else None
                zipc = (a.get("PSTLZIP5") or "").strip() or None
                if not cnt_assd and not zipc:
                    continue
                batch.append({"parcel_id": parcel_id, "zip": zipc, "cnt_assd": cnt_assd})

            if len(batch) >= 1000:
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """UPDATE properties SET
                                   postal_code = COALESCE(postal_code, %(zip)s),
                                   assessed_value = COALESCE(%(cnt_assd)s, assessed_value),
                                   updated_at = NOW()
                               WHERE county_id='bradley' AND parcel_id = %(parcel_id)s""",
                            batch)
                    conn.commit()
                stats["updated"] += len(batch)
                log.info("  processed %d...", stats["updated"])
                batch = []

        if batch:
            if not dry_run:
                with conn.cursor() as cur:
                    cur.executemany(
                        """UPDATE properties SET
                               postal_code = COALESCE(postal_code, %(zip)s),
                               assessed_value = COALESCE(%(cnt_assd)s, assessed_value),
                               updated_at = NOW()
                           WHERE county_id='bradley' AND parcel_id = %(parcel_id)s""",
                        batch)
                conn.commit()
            stats["updated"] += len(batch)
    finally:
        conn.close()

    # Post-checks
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM properties WHERE county_id='bradley' AND postal_code IS NOT NULL")
            stats["zip_filled"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM properties WHERE county_id='bradley' AND assessed_value IS NOT NULL")
            stats["with_assd_after"] = cur.fetchone()[0]
    finally:
        conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Bradley tax-roll enrich (zips + current values)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        stream=sys.stdout)
    stats = run(args.dry_run)
    print(f"\n{'='*50}")
    print("Results:")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
