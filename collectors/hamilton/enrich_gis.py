#!/usr/bin/env python3
"""
Batch enrich properties with GIS data via ArcGIS REST API.

Uses the Live_RealProperty service (replacement for Live_Parcels):
  https://mapsdev.hamiltontn.gov/hcwa03/rest/services/Live_RealProperty/MapServer

Fields mapped:
  lot_size_sqft    <- MAPACRES × 43560 (or CALCACRES × 43560, or DEEDACRES × 43560)
  building_value   <- BUILDVALUE
  land_value       <- LANDVALUE
  total_value      <- APPVALUE (appraised/total value)
  assessed_value   <- ASSVALUE (tax assessment base)
  neighborhood     <- NEIGHCODE
  tax_district     <- DISTRICT
  property_type_code <- PROPTYPE
  legal_description <- LEGALDESC1 (concatenated LEGALDESC1-8)
  sale_history     <- SALE1-4 date+price as JSONB array
  county_tax_amount  <- CNYTAXAMT
  city_tax_amount    <- MUNTAXAMT
  tax_bill_number  <- TXBILLNUM
  owner_name       <- OWNERNAME1 (+ OWNERNAME2)
  mailing_address  <- MASTNUM + MASTNAME + MATYPESFX + MALINE2 + MACITY + MASTATE + MAZIP
  mailing_zip      <- MAZIP

The old Live_Parcels service is dead (500 error).
Live_RealProperty uses TAX_MAP_NO for parcel lookup (matches our parcel_id format).

Usage:
  python3 enrich_gis.py --mode null-lots --batch-size 50
  python3 enrich_gis.py --mode all --batch-size 50
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any

import psycopg2
import psycopg2.extras
import requests
import os

# ── Config ──────────────────────────────────────────────────────────────────

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

GIS_URL = (
    "https://mapsdev.hamiltontn.gov/hcwa03/rest/services/"
    "Live_RealProperty/MapServer/0/query"
)

# ArcGIS epoch is milliseconds since 1970-01-01
ARC_EPOCH = 1e3

# Fields we request from GIS
OUT_FIELDS = (
    "LANDVALUE,BUILDVALUE,APPVALUE,ASSVALUE,DISTRICT,NEIGHCODE,PROPTYPE,"
    "LEGALDESC1,LEGALDESC2,LEGALDESC3,LEGALDESC4,LEGALDESC5,LEGALDESC6,"
    "LEGALDESC7,LEGALDESC8,MAPACRES,CALCACRES,DEEDACRES,TAX_MAP_NO,"
    "MUNTAXAMT,CNYTAXAMT,TXBILLNUM,LYRBILLNUM,BCKTAXYRS,"
    "SALE1DATE,SALE1CONSD,SALE1BOOK,SALE1PAGE,SALE1TYPE,SALE1CONF,"
    "SALE2DATE,SALE2CONSD,SALE2BOOK,SALE2PAGE,SALE2TYPE,SALE2CONF,"
    "SALE3DATE,SALE3CONSD,SALE3BOOK,SALE3PAGE,SALE3TYPE,SALE3CONF,"
    "SALE4DATE,SALE4CONSD,SALE4BOOK,SALE4PAGE,SALE4TYPE,SALE4CONF,"
    "OWNERNAME1,OWNERNAME2,STNUM,DIRPFX,STNAME,TYPESFX,ADDRESS,"
    "MASTNUM,MADIRPFX,MASTNAME,MATYPESFX,MALINE2,MACITY,MASTATE,MAZIP,"
    "LOTDIM1,LOTDIM2,LUCODE,IMPCODE,EXEMPTCODE,CONTROLMAP,PBA_NUM,PARCEL,"
    "MAP,GROUP_,SUBDIVNAME,PLAT1BOOK,PLAT1PAGE,PLAT1PG,PLAT1LOT,"
    "PLAT2BOOK,PLAT2PAGE,PLAT2PG,PLAT2LOT,TAXRLFCODE,RecordsOnl,"
    "ApproxAddr,ApproxSize,Zoning,PropertyLi,TotPropert,MinSubTot,MinBid"
)

log = logging.getLogger("gis_enrich")

# ── GIS helpers ─────────────────────────────────────────────────────────────

def arcgis_ms_to_date(ms: float | None) -> str | None:
    """Convert ArcGIS epoch-ms to YYYY-MM-DD."""
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / ARC_EPOCH).strftime("%Y-%m-%d")
    except Exception:
        return None


def build_sale_history(attrs: dict) -> list[dict] | None:
    """Build sale_history array from SALE1-4 fields."""
    sales: list[dict] = []
    for i in range(1, 5):
        date_ms = attrs.get(f"SALE{i}DATE")
        price = attrs.get(f"SALE{i}CONSD")
        book = attrs.get(f"SALE{i}BOOK") or ""
        page = attrs.get(f"SALE{i}PAGE") or ""
        stype = attrs.get(f"SALE{i}TYPE") or ""
        conf = attrs.get(f"SALE{i}CONF") or ""

        sale_date = arcgis_ms_to_date(date_ms) if date_ms else None
        if price and price > 0:
            sales.append({
                "date": sale_date,
                "price": float(price),
                "book": book.strip(),
                "page": page.strip(),
                "type": stype.strip(),
                "confirmed": conf.strip(),
            })
    return sales if sales else None


def fetch_gis_batch(parcel_ids: list[str], delay: float = 0.3) -> dict[str, dict]:
    """
    Fetch GIS data for a batch of parcel IDs.
    Returns dict mapping parcel_id -> attrs.
    Uses TAX_MAP_NO field for lookup.
    """
    results: dict[str, dict] = {}

    # Query in sub-batches of 5 to avoid URL length issues
    sub_batch_size = 5
    for i in range(0, len(parcel_ids), sub_batch_size):
        sub = parcel_ids[i:i + sub_batch_size]

        # Build OR condition for TAX_MAP_NO
        clauses = " OR ".join(
            f"TAX_MAP_NO = '{pid.replace(chr(39), chr(39)*2)}'" for pid in sub
        )

        params = {
            "where": clauses,
            "outFields": OUT_FIELDS,
            "f": "json",
        }

        try:
            resp = requests.get(GIS_URL, params=params, timeout=30)
            data = resp.json()

            if "features" in data:
                for feature in data["features"]:
                    attrs = feature.get("attributes", {})
                    tax_map_no = attrs.get("TAX_MAP_NO", "").strip()
                    if tax_map_no:
                        results[tax_map_no] = attrs
        except Exception as e:
            log.warning(f"GIS fetch failed for batch: {e}")

        time.sleep(delay)

    return results


# ── DB helpers ──────────────────────────────────────────────────────────────

def get_parcels(conn, mode: str, limit: int | None = None) -> list[str]:
    """Return parcel_ids to enrich based on mode."""
    if mode == "null-lots":
        # Skip sub-parcels (L000, C001 etc.) — they won't match GIS
        sql = """
            SELECT parcel_id FROM properties
            WHERE lot_size_sqft IS NULL
              AND parcel_id NOT LIKE '% L%'
              AND parcel_id NOT LIKE '% C%'
            ORDER BY id
        """
    elif mode == "null-values":
        sql = """
            SELECT parcel_id FROM properties
            WHERE building_value IS NULL
              AND parcel_id NOT LIKE '% L%'
              AND parcel_id NOT LIKE '% C%'
            ORDER BY id
        """
    elif mode == "all":
        sql = "SELECT parcel_id FROM properties ORDER BY id"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    if limit:
        sql += f" LIMIT {limit}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [row["parcel_id"] for row in cur.fetchall()]


def update_property(conn, parcel_id: str, data: dict) -> bool:
    """Update a property with GIS data. Returns True if updated."""
    set_clauses = []
    values = []
    idx = 1

    field_map = {
        "lot_size_sqft": "LOT_SIZE_SQFT",
        "building_value": "BUILDVALUE",
        "land_value": "LANDVALUE",
        "total_value": "APPVALUE",
        "assessed_value": "ASSVALUE",
        "neighborhood": "NEIGHCODE",
        "tax_district": "DISTRICT",
        "property_type_code": "PROPTYPE",
        "legal_description": "LEGAL_DESC",
        "sale_history": "SALE_HISTORY",
        "county_tax_amount": "CNYTAXAMT",
        "city_tax_amount": "MUNTAXAMT",
        "tax_bill_number": "TXBILLNUM",
        "owner_name": "OWNER_NAME",
        "mailing_address": "MAILING_ADDR",
        "mailing_zip": "MAZIP",
    }

    for db_field, src_key in field_map.items():
        val = data.get(src_key)
        if val is not None and val != "":
            set_clauses.append(f"{db_field} = %s")
            values.append(val)
            idx += 1

    if not set_clauses:
        return False

    set_clauses.append("updated_at = NOW()")
    values.append(parcel_id)

    sql = f"""
        UPDATE properties
        SET {', '.join(set_clauses)}
        WHERE parcel_id = %s
    """

    with conn.cursor() as cur:
        cur.execute(sql, values)
    conn.commit()
    return True


def process_gis_attrs(attrs: dict) -> dict:
    """Process raw GIS attributes into DB-ready format."""
    result = {}

    # Lot size — try MAPACRES, then CALCACRES, then DEEDACRES
    acres = None
    for key in ["MAPACRES", "CALCACRES", "DEEDACRES"]:
        val = attrs.get(key)
        if val and val > 0:
            acres = val
            break
    if acres and acres > 0:
        result["LOT_SIZE_SQFT"] = round(acres * 43560, 2)

    # Values
    for field, src in [
        ("BUILDVALUE", "BUILDVALUE"),
        ("LANDVALUE", "LANDVALUE"),
        ("APPVALUE", "APPVALUE"),
        ("ASSVALUE", "ASSVALUE"),
        ("CNYTAXAMT", "CNYTAXAMT"),
        ("MUNTAXAMT", "MUNTAXAMT"),
    ]:
        val = attrs.get(src)
        if val and val > 0:
            result[field] = float(val)

    # Text fields
    for field, src in [
        ("NEIGHCODE", "NEIGHCODE"),
        ("DISTRICT", "DISTRICT"),
        ("PROPTYPE", "PROPTYPE"),
        ("TXBILLNUM", "TXBILLNUM"),
        ("MAZIP", "MAZIP"),
    ]:
        val = attrs.get(src)
        if val and str(val).strip():
            result[field] = str(val).strip()

    # Legal description — concatenate LEGALDESC1-8
    legal_parts = []
    for i in range(1, 9):
        desc = attrs.get(f"LEGALDESC{i}")
        if desc and str(desc).strip():
            legal_parts.append(str(desc).strip())
    if legal_parts:
        result["LEGAL_DESC"] = " ".join(legal_parts)

    # Owner name
    owner1 = (attrs.get("OWNERNAME1") or "").strip()
    owner2 = (attrs.get("OWNERNAME2") or "").strip()
    if owner1:
        result["OWNER_NAME"] = owner1 + (" " + owner2 if owner2 else "")

    # Mailing address
    mail_parts = []
    for key in ["MASTNUM", "MADIRPFX", "MASTNAME", "MATYPESFX", "MALINE2"]:
        val = (attrs.get(key) or "").strip()
        if val:
            mail_parts.append(val)
    city = (attrs.get("MACITY") or "").strip()
    state = (attrs.get("MASTATE") or "").strip()
    zipcode = (attrs.get("MAZIP") or "").strip()
    if mail_parts:
        addr = " ".join(mail_parts)
        if city:
            addr += f", {city}"
        if state:
            addr += f", {state}"
        if zipcode:
            addr += f" {zipcode}"
        result["MAILING_ADDR"] = addr

    # Sale history
    sales = build_sale_history(attrs)
    if sales:
        result["SALE_HISTORY"] = json.dumps(sales)

    return result


# ── Main ────────────────────────────────────────────────────────────────────

def run_enrichment(mode: str, batch_size: int, limit: int | None, delay: float, dry_run: bool):
    """Main enrichment loop."""
    conn = psycopg2.connect(db_dsn())
    try:
        parcel_ids = get_parcels(conn, mode, limit)
        total = len(parcel_ids)
        log.info(f"Found {total} parcels to enrich")

        if total == 0:
            log.info("No parcels need enrichment")
            return

        # Fetch GIS data in batches
        all_gis: dict[str, dict] = {}
        log.info("Phase 1: Fetching GIS data...")
        for i in range(0, total, batch_size):
            batch = parcel_ids[i:i + batch_size]
            batch_results = fetch_gis_batch(batch, delay=delay)
            all_gis.update(batch_results)
            fetched = len(all_gis)
            log.info(f"  Fetched {fetched}/{total} ({100*fetched/total:.1f}%)")

        log.info(f"GIS fetch complete: {len(all_gis)} parcels matched")

        # Update DB
        if dry_run:
            log.info("Dry run — skipping DB updates")
            return

        log.info("Phase 2: Updating database...")
        updated = 0
        skipped = 0
        for parcel_id, attrs in all_gis.items():
            data = process_gis_attrs(attrs)
            if update_property(conn, parcel_id, data):
                updated += 1
            else:
                skipped += 1
            if updated % 1000 == 0:
                log.info(f"  Updated {updated}/{len(all_gis)}")

        log.info(f"Enrichment complete: {updated} updated, {skipped} skipped")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="GIS enrichment script")
    parser.add_argument("--mode", default="null-lots", choices=["null-lots", "null-values", "all"])
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    run_enrichment(args.mode, args.batch_size, args.limit, args.delay, args.dry_run)


if __name__ == "__main__":
    main()
