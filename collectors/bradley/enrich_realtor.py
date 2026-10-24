#!/usr/bin/env python3
"""
Enrich Bradley County properties from Realtor.com (via HomeHarvest).

Scrapes for-sale + sold listings in Cleveland, TN (the Bradley County MLS zone),
matches to our rows by normalized address (scoped to county_id='bradley'),
upserts bedrooms/bathrooms/style/stories/listing/estimated-value fields.

Match strategy (same as collectors/enrich_bedbath.py but county-scoped):
  1. exact: upper(properties.address) = normalized listing address
  2. fuzzy: house-number + street-token LIKE, only rows missing beds/baths

Usage:
  .venv/bin/python collectors/bradley/enrich_realtor.py --dry-run --limit 20
  .venv/bin/python collectors/bradley/enrich_realtor.py
"""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg2
from homeharvest import scrape_property

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

log = logging.getLogger("bradley.realtor")

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit@localhost:5432/proptoit")

DIR_MAP = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
SUFFIX_MAP = {
    "AVENUE": "AVE", "STREET": "ST", "ROAD": "RD", "BOULEVARD": "BLVD",
    "LANE": "LN", "DRIVE": "DR", "COURT": "CT", "PLACE": "PL",
    "CIRCLE": "CIR", "TRAIL": "TRL", "PARKWAY": "PKWY", "PIKE": "PIKE",
    "HEIGHTS": "HTS", "MOUNTAIN": "MTN", "RIDGE": "RDG", "CREEK": "CRK",
    "VALLEY": "VLY", "JUNCTION": "JCT", "MILL": "ML", "CROSSING": "XING",
}
UNIT_SUFFIXES = {"UNIT", "APT", "APARTMENT", "SUITE", "STE", "BUILDING", "BLDG", "PH"}


def normalize_address(addr: str) -> str:
    if not addr:
        return ""
    addr = re.sub(
        r",?\s*(?:Cleveland|Charleston|Georgetown|Bradley\s*County|"
        r"Ooltewah|McDonald|Old\s*Fort|Wildwood\s*Lake|Waterville)\s*,?\s*"
        r"(?:TN|Tennessee)?\s*,?\s*(?:\d{5}(?:-\d{4})?)?\s*$",
        "", addr, flags=re.IGNORECASE,
    )
    for suffix in UNIT_SUFFIXES:
        addr = re.sub(rf"\s*{suffix}\s*\d+", "", addr, flags=re.IGNORECASE)
    addr = addr.strip(", ").replace(",", " ")
    addr = re.sub(r"\s+", " ", addr).strip()
    for full, abbr in DIR_MAP.items():
        addr = re.sub(rf"\b{full}\b", abbr, addr, flags=re.IGNORECASE)
    for full, abbr in SUFFIX_MAP.items():
        addr = re.sub(rf"\b{full}\b", abbr, addr, flags=re.IGNORECASE)
    return addr.upper()


def safe_float(val):
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val):
    if pd.isna(val):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def safe_date(val):
    if pd.isna(val):
        return None
    try:
        ts = pd.Timestamp(val)
        return None if pd.isna(ts) else ts.date().isoformat()
    except (ValueError, TypeError):
        return None


def safe_str(val):
    return None if pd.isna(val) else str(val)


def safe_bool(val):
    return None if pd.isna(val) else bool(val)


UPSERT_SQL = """
    UPDATE properties
    SET bedrooms          = COALESCE(%(beds)s, properties.bedrooms),
        bathrooms         = COALESCE(%(baths)s, properties.bathrooms),
        year_built        = COALESCE(%(year)s, properties.year_built),
        building_sqft     = COALESCE(properties.building_sqft, %(sqft)s),
        lot_size_sqft     = COALESCE(properties.lot_size_sqft, %(lot)s),
        stories           = COALESCE(%(stories)s, properties.stories),
        property_style    = COALESCE(%(style)s, properties.property_style),
        parking_garage    = COALESCE(%(parking)s, properties.parking_garage),
        hoa_fee           = COALESCE(%(hoa)s, properties.hoa_fee),
        new_construction  = COALESCE(%(newcon)s, properties.new_construction),
        list_price        = COALESCE(%(list_price)s, properties.list_price),
        list_date         = COALESCE(%(list_date)s, properties.list_date),
        listing_status    = COALESCE(%(status)s, properties.listing_status),
        days_on_mls       = COALESCE(%(dom)s, properties.days_on_mls),
        estimated_value   = COALESCE(%(est)s, properties.estimated_value),
        last_sold_price   = COALESCE(%(last_sold_price)s, properties.last_sold_price),
        last_sold_date    = COALESCE(%(last_sold_date)s, properties.last_sold_date),
        price_per_sqft    = COALESCE(%(ppsf)s, properties.price_per_sqft),
        updated_at        = NOW()
    WHERE county_id = 'bradley'
      AND id = (SELECT id FROM properties
                WHERE county_id = 'bradley' AND upper(address) = %(norm_addr)s
                LIMIT 1)
    RETURNING id, address;
"""

FUZZY_SQL = """
    UPDATE properties
    SET bedrooms          = COALESCE(%(beds)s, properties.bedrooms),
        bathrooms         = COALESCE(%(baths)s, properties.bathrooms),
        year_built        = COALESCE(%(year)s, properties.year_built),
        building_sqft     = COALESCE(properties.building_sqft, %(sqft)s),
        lot_size_sqft     = COALESCE(properties.lot_size_sqft, %(lot)s),
        stories           = COALESCE(%(stories)s, properties.stories),
        property_style    = COALESCE(%(style)s, properties.property_style),
        parking_garage    = COALESCE(%(parking)s, properties.parking_garage),
        hoa_fee           = COALESCE(%(hoa)s, properties.hoa_fee),
        new_construction  = COALESCE(%(newcon)s, properties.new_construction),
        list_price        = COALESCE(%(list_price)s, properties.list_price),
        list_date         = COALESCE(%(list_date)s, properties.list_date),
        listing_status    = COALESCE(%(status)s, properties.listing_status),
        days_on_mls       = COALESCE(%(dom)s, properties.days_on_mls),
        estimated_value   = COALESCE(%(est)s, properties.estimated_value),
        last_sold_price   = COALESCE(%(last_sold_price)s, properties.last_sold_price),
        last_sold_date    = COALESCE(%(last_sold_date)s, properties.last_sold_date),
        price_per_sqft    = COALESCE(%(ppsf)s, properties.price_per_sqft),
        updated_at        = NOW()
    WHERE county_id = 'bradley'
      AND upper(address) LIKE %(pattern)s
      AND (bedrooms IS NULL OR bathrooms IS NULL)
      -- deterministic + unique-match only: previously an arbitrary row was
      -- chosen (no ORDER BY) when several addresses matched the loose
      -- {num}%{rest}% pattern, so listing data could land on the wrong parcel.
      AND id = (SELECT min(id) FROM properties
                WHERE county_id = 'bradley'
                  AND upper(address) LIKE %(pattern)s
                  AND (bedrooms IS NULL OR bathrooms IS NULL))
      AND (SELECT count(*) FROM properties
           WHERE county_id = 'bradley'
             AND upper(address) LIKE %(pattern)s) = 1
    RETURNING id, address;
"""


def row_to_params(row) -> dict | None:
    beds = safe_int(row.get("beds"))
    full_baths = safe_float(row.get("full_baths"))
    half_baths = safe_float(row.get("half_baths"))
    baths = full_baths
    if half_baths:
        baths = (baths or 0) + 0.5 * half_baths
    if beds is None and baths is None:
        return None
    norm = normalize_address(row.get("formatted_address", ""))
    if not norm:
        return None
    return {
        "norm_addr": norm,
        "beds": beds,
        "baths": baths,
        "year": safe_int(row.get("year_built")),
        "sqft": safe_float(row.get("sqft")),
        "lot": safe_float(row.get("lot_sqft")),
        "stories": safe_int(row.get("stories")),
        "style": safe_str(row.get("style")),
        "parking": safe_int(row.get("parking_garage")),
        "hoa": safe_float(row.get("hoa_fee")),
        "newcon": safe_bool(row.get("new_construction")),
        "list_price": safe_float(row.get("list_price")),
        "list_date": safe_date(row.get("list_date")),
        "status": safe_str(row.get("status")),
        "dom": safe_int(row.get("days_on_mls")),
        "est": safe_float(row.get("estimated_value")),
        "last_sold_price": safe_float(row.get("last_sold_price")),
        "last_sold_date": safe_date(row.get("last_sold_date")),
        "ppsf": safe_float(row.get("price_per_sqft")),
    }


def upsert_row(conn, row, dry_run: bool) -> bool:
    params = row_to_params(row)
    if params is None:
        return False
    if dry_run:
        log.info("[DRY] %s -> %s bed %s bath est=%s",
                 row.get("formatted_address"), params["beds"], params["baths"], params["est"])
        return True
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, params)
        hit = cur.fetchone()
        if hit:
            conn.commit()
            return True
        # fuzzy fallback
        m = re.match(r"(\d+)", params["norm_addr"])
        if not m:
            return False
        num, rest = m.group(1), params["norm_addr"][len(m.group(1)):].strip()
        cur.execute(FUZZY_SQL, {**params, "pattern": f"{num}%{rest}%"})
        hit = cur.fetchone()
        if hit:
            conn.commit()
            return True
    return False


def run(dry_run: bool, limit: int, listing_types: list[str]):
    stats = {"scraped": 0, "matched": 0, "empty_batches": 0}
    conn = psycopg2.connect(DB_DSN) if not dry_run else None
    try:
        for lt in listing_types:
            log.info("Scraping %s listings in Cleveland, TN (up to 5000)...", lt)
            props = scrape_property(location="Cleveland, TN", listing_type=lt, limit=5000)
            if props.empty:
                log.info("  No %s listings found", lt)
                stats["empty_batches"] += 1
                continue
            log.info("  Found %d %s listings", len(props), lt)
            stats["scraped"] += len(props)
            for _, row in props.iterrows():
                if limit and stats["matched"] >= limit:
                    break
                if upsert_row(conn, row, dry_run):
                    stats["matched"] += 1
            if limit and stats["matched"] >= limit:
                break
    finally:
        if conn:
            conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Bradley County Realtor.com enrichment")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--type", default="for_sale,sold")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        stream=sys.stdout)

    listing_types = [t.strip().upper() for t in args.type.split(",")]
    stats = run(args.dry_run, args.limit, listing_types)
    print(f"\n{'='*50}")
    print("Results:")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
