#!/usr/bin/env python3
"""
Reindex all properties into Meilisearch — streams NDJSON in chunks
(replaces the single-giant-array curl approach in meili_indexer.sh,
which silently truncated the 247MB body on the full 178k-row export).

Usage:
  .venv/bin/python collectors/index_to_meili.py            # full reindex
  .venv/bin/python collectors/index_to_meili.py --county bradley
  .venv/bin/python collectors/index_to_meili.py --delete-first
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MEILI_URL = "http://localhost:7700"
CHUNK = 5000
SELECT_SQL = """
    SELECT id, parcel_id, address, city, state, postal_code,
        lot_size_sqft, building_sqft, year_built, bedrooms, bathrooms,
        last_sale_price, last_sale_date, assessed_value, building_value,
        land_value, total_value, tax_amount, owner_of_record,
        owner_mailing_address, owner_occupant, lat, lng,
        is_vacant, is_tax_delinquent, distress_score, deal_score,
        pre_foreclosure_flag, code_violation_flag, probate_flag, fsbo_flag,
        vacancy_heuristic, property_category, property_type_code, county_id
    FROM properties {where} ORDER BY id
"""

log = logging.getLogger("meili_index")


def stream_docs(dsn: str, where: str, chunk: int = CHUNK):
    conn = psycopg2.connect(dsn)
    name = "properties"
    cur = conn.cursor(name=f"meili_index_{name}")
    cur.itersize = chunk
    cur.execute(SELECT_SQL.format(where=where))
    rows = cur.fetchmany(chunk)
    cols = [d[0] for d in cur.description]
    while rows:
        batch = []
        for r in rows:
            d = dict(zip(cols, r))
            d["address"] = f"{d['address']}, {d['city']}, {d['state']}"
            # dates/decimals -> json-safe
            for k in ("last_sale_date",):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            for k, v in list(d.items()):
                if hasattr(v, "as_integer_ratio"):  # Decimal
                    d[k] = float(v)
            d["id"] = int(d["id"])  # Decimal ids serialize as float -> invalid key
            batch.append(d)
        yield batch
        rows = cur.fetchmany(chunk)
    cur.close()
    conn.close()


def run(county: str | None, delete_first: bool):
    key = os.environ.get("MEILI_MASTER_KEY")
    if not key:
        raise SystemExit("MEILI_MASTER_KEY required")
    h = {"Authorization": f"Bearer {key}"}
    where = "WHERE county_id = '%s'" % county if county else ""
    dsn = os.environ["DATABASE_URL"]

    requests.put(f"{MEILI_URL}/indexes/properties",
                 headers=h, json={"primaryKey": "id"}, timeout=30)

    if delete_first and not county:
        r = requests.delete(f"{MEILI_URL}/indexes/properties/documents", headers=h, timeout=30)
        log.info("Delete-all: %s", r.status_code)

    total = 0
    for batch in stream_docs(dsn, where):
        nd = "\n".join(json.dumps(d, default=str) for d in batch)
        r = requests.post(
            f"{MEILI_URL}/indexes/properties/documents?primaryKey=id",
            headers={**h, "Content-Type": "application/x-ndjson"},
            data=nd, timeout=120)
        r.raise_for_status()
        total += len(batch)
        if total % 20000 < CHUNK:
            log.info("  queued %d...", total)
    log.info("Queued %d docs total", total)
    return total


def wait_and_report(county: str | None):
    key = os.environ["MEILI_MASTER_KEY"]
    h = {"Authorization": f"Bearer {key}"}
    import time
    for _ in range(60):
        time.sleep(5)
        s = requests.get(f"{MEILI_URL}/indexes/properties/stats", headers=h, timeout=10).json()
        if not s.get("isIndexing"):
            break
    else:
        s = {"numberOfDocuments": -1, "note": "still indexing after 300s"}
    print("Index docs:", s["numberOfDocuments"])
    if county:
        r = requests.get(f"{MEILI_URL}/indexes/properties/search", headers=h, timeout=10,
                         json={"q": "", "filter": f"county_id = {county}", "limit": 1})
        print(f"{county} hits:", r.json().get("estimatedTotalHits"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", default=None)
    parser.add_argument("--delete-first", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s", stream=sys.stdout)
    run(args.county, args.delete_first)
    wait_and_report(args.county)


if __name__ == "__main__":
    main()
