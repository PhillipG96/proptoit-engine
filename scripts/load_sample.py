#!/usr/bin/env python3
"""Load the bundled Hamilton County sample dataset into the local sample DB.

Idempotent: truncates properties/distress_signals/opt_out_requests, then
re-inserts everything from sample/hamilton_subset.json. Safe to re-run.

Usage (normally invoked by `make dev`):
    python scripts/load_sample.py

Environment:
    DATABASE_URL  DSN of the target DB (default: the sample stack on :5437)
"""
import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "sample" / "hamilton_subset.json"

DEFAULT_DSN = "postgresql://proptoit@localhost:5437/proptoit"

# Flag columns derived from the parcel's distress-signal rows.
SIGNAL_FLAG = {
    "pre_foreclosure": "pre_foreclosure_flag",
    "code_violation": "code_violation_flag",
    "probate": "probate_flag",
    "fsbo": "fsbo_flag",
    "vacancy": "vacancy_heuristic",  # also is_vacant
}
TAX_SIGNAL = "tax_delinquent"


def main() -> None:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    parcels = json.loads(SAMPLE.read_text())
    n_signals = sum(len(p["signals"]) for p in parcels)
    print(f"Sample: {len(parcels)} parcels, {n_signals} distress signals")

    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE distress_signals, opt_out_requests, properties RESTART IDENTITY CASCADE"
                )

                rows = []
                for p in parcels:
                    types = {s["signal_type"] for s in p["signals"]}
                    flags = {flag: (sig in types) for sig, flag in SIGNAL_FLAG.items()}
                    point = f"({p['lng']},{p['lat']})" if p.get("lat") is not None else None
                    rows.append((
                        p["county_id"], p["parcel_id"], p["address"], p["city"], p["state"],
                        p["postal_code"], p["lot_size_sqft"], p["building_sqft"],
                        p["year_built"], p["bedrooms"], p["bathrooms"],
                        p["last_sale_price"], p["last_sale_date"], p["assessed_value"],
                        p["tax_amount"],
                        p["owner_of_record"], p["owner_mailing_address"],  # always NULL
                        p["owner_occupant"],
                        p["lat"], p["lng"], point,
                        flags["pre_foreclosure_flag"], flags["code_violation_flag"],
                        flags["probate_flag"], flags["fsbo_flag"], flags["vacancy_heuristic"],
                        flags["vacancy_heuristic"],  # is_vacant
                        TAX_SIGNAL in types,          # is_tax_delinquent
                        p["distress_score"], p["deal_score"],
                    ))
                execute_values(cur, """
                    INSERT INTO properties (
                        county_id, parcel_id, address, city, state, postal_code,
                        lot_size_sqft, building_sqft, year_built, bedrooms, bathrooms,
                        last_sale_price, last_sale_date, assessed_value, tax_amount,
                        owner_of_record, owner_mailing_address, owner_occupant,
                        lat, lng, geocoords_point,
                        pre_foreclosure_flag, code_violation_flag, probate_flag,
                        fsbo_flag, vacancy_heuristic, is_vacant, is_tax_delinquent,
                        distress_score, deal_score
                    ) VALUES %s
                """, rows, page_size=500,
                    template="(" + ",".join(["%s"] * 20 + ["%s::point"] + ["%s"] * 9) + ")")

                # Map parcel_id -> new serial id for signal FKs.
                cur.execute("SELECT id, parcel_id FROM properties")
                id_by_parcel = {parcel: pid for pid, parcel in cur.fetchall()}

                sig_rows = []
                seen = set()
                for p in parcels:
                    for s in p["signals"]:
                        key = (p["parcel_id"], s["signal_type"])
                        if key in seen:  # prod allows dupes; carve schema forbids them
                            continue
                        seen.add(key)
                        sig_rows.append((
                            id_by_parcel[p["parcel_id"]],
                            s["signal_type"],
                            s["detected_at"],
                            None,            # source_url (prod has none)
                            None,            # notes
                            s["source"],
                            s["severity"],
                            float(s["confidence"]) if s["confidence"] is not None else None,
                            json.dumps(s["metadata"]) if s["metadata"] is not None else None,
                        ))
                execute_values(cur, """
                    INSERT INTO distress_signals (
                        property_id, signal_type, detected_at, source_url, notes,
                        source, severity, confidence, metadata
                    ) VALUES %s
                """, sig_rows, page_size=500)

                cur.execute("SELECT count(*) FROM properties")
                n_prop = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM distress_signals")
                n_sig = cur.fetchone()[0]
        conn.close()
    except Exception:
        conn.close()
        raise

    print(f"Loaded {n_prop} properties, {n_sig} distress signals into {dsn}")
    # opt_out_requests ships intentionally EMPTY (see sample/README.md).


if __name__ == "__main__":
    sys.exit(main())
