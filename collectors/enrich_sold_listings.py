#!/usr/bin/env python3
"""
Enrich properties with sold listing data from Realtor.com (via HomeHarvest).
Scrapes sold listings year-by-year (2016-2025) for Chattanooga and Hamilton County, TN.

Fields captured:
  - bedrooms, bathrooms
  - year_built, lot_sqft, stories, property_style
  - parking_garage, hoa_fee, new_construction
  - list_price, list_date, listing_status, days_on_mls
  - estimated_value, last_sold_price, last_sold_date, price_per_sqft
  - sold_price

Usage:
  python3 enrich_sold_listings.py [--dry-run] [--limit 100] [--start-year 2020] [--end-year 2025]
"""

import argparse
import logging
import os
import re
import sys

import pandas as pd
import psycopg2
from homeharvest import scrape_property

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("sold_enrich")

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

DIR_MAP = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

SUFFIX_MAP = {
    "AVENUE": "AVE", "STREET": "ST", "ROAD": "RD", "BOULEVARD": "BLVD",
    "LANE": "LN", "DRIVE": "DR", "COURT": "CT", "PLACE": "PL",
    "WAY": "WAY", "CIRCLE": "CIR", "TRAIL": "TRL", "TERMINAL": "TER",
    "HEIGHTS": "HTS", "PARKWAY": "PKWY", "PIKE": "PIKE",
    "MOUNTAIN": "MTN", "MOUNT": "MT", "GAP": "GAP", "RIDGE": "RDG",
    "CREEK": "CRK", "FERRY": "FRY", "HOLLOW": "HLW", "GROVE": "GRV",
    "MEADOWS": "MEWS", "MEADOW": "MEW", "JUNCTION": "JCT",
    "VALLEY": "VLY", "WOODS": "WDS", "WOOD": "WD",
    "GARDENS": "GDNS", "GARDEN": "GDN", "FIELDS": "FLDS", "FIELD": "FLD",
    "DALE": "DALE", "DALES": "DALES", "GLEN": "GLEN", "GLENS": "GLENS",
    "TRAILS": "TRLS", "CREEKS": "CRKS", "FORK": "FRK", "FORKS": "FRKS",
    "MILL": "ML", "MILLS": "MLS", "SUMMIT": "SUM", "PARK": "PARK",
    "FOREST": "FRST", "HARBOR": "HRB", "PORT": "PORT", "HILL": "HILL",
    "VIEW": "VIEW", "VIEWS": "VIEWS",
}

ORDINAL_MAP = {
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH",
    "FIFTH": "5TH", "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH",
    "NINTH": "9TH", "TENTH": "10TH", "ELEVENTH": "11TH", "TWELFTH": "12TH",
    "THIRTEENTH": "13TH", "FOURTEENTH": "14TH", "FIFTEENTH": "15TH",
    "SIXTEENTH": "16TH", "SEVENTEENTH": "17TH", "EIGHTEENTH": "18TH",
    "NINETEENTH": "19TH", "TWENTIETH": "20TH",
    "TWENTY-FIRST": "21ST", "TWENTY-SECOND": "22ND", "TWENTY-THIRD": "23RD",
    "TWENTY-FOURTH": "24TH", "TWENTY-FIFTH": "25TH",
    "THIRTIETH": "30TH", "FORTIETH": "40TH", "FIFTIETH": "50TH",
    "SIXTIETH": "60TH", "SEVENTIETH": "70TH", "EIGHTIETH": "80TH",
    "NINETIETH": "90TH", "HUNDREDTH": "100TH",
}

UNIT_SUFFIXES = {
    "UNIT", "APT", "APARTMENT", "SUITE", "STE", "#", "BUILDING",
    "BLDG", "PH", "PHASE", "CONDO", "TOWNHOUSE", "TOWNHOME",
}


def normalize_address(addr):
    if not addr:
        return ""
    addr = re.sub(
        r",?\s*(?:Chattanooga|Hixson|Eagle\s*Valley|Ooltewah|"
        r"Lookout\s*Mountain|Signal\s*Mountain|Rock\s*Island|"
        r"Fort\s*Loudoun|Cloudcroft|Finley|Apison|"
        r"Bristol\s*Ridge|Hiwasee|St?\s*Elmo|Ocoee|Coker|"
        r"Waltersville|Cumberland\s*Gap|Cleveland|Harrison|Loudon|"
        r"Vonore|Maryville|Knoxville|Morristown|Bristol|"
        r"Johnson\s*City|Greeneville|Bullard|Athens|Kingston|"
        r"Farragut|Powell|Alcoa|Wartburg|Halls\s*Crossing|"
        r"Crossville|Cowan|Decatur|Spencer|Manchester|Oneida|"
        r"Pine\s*Tops|Signal\s*Mtn|Rock\s*Island|"
        r"Fort\s*Loudoun|Cloudcroft|Finley|Apison|"
        r"Bristol\s*Ridge|Hiwasee|St?\s*Elmo|Ocoee|"
        r"Coker|Waltersville)\s*,?\s*(?:TN|Tennessee)?\s*,?\s*(?:\d{5}(?:-\d{4})?)?\s*$",
        "", addr, flags=re.IGNORECASE,
    )
    for suffix in UNIT_SUFFIXES:
        addr = re.sub(rf"\s*{suffix}\s*\d+", "", addr, flags=re.IGNORECASE)
    addr = addr.strip(", ")
    addr = addr.replace(",", " ")
    addr = re.sub(r"\s+", " ", addr).strip()
    for full, abbr in ORDINAL_MAP.items():
        addr = re.sub(rf"\b{full}\b", abbr, addr, flags=re.IGNORECASE)
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
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except (ValueError, TypeError):
        return None


def safe_str(val):
    if pd.isna(val):
        return None
    return str(val)


def safe_bool(val):
    if pd.isna(val):
        return None
    return bool(val)


UPSERT_SQL = """
    WITH matched AS (
        SELECT id, address FROM properties
        WHERE upper(address) = %s
    )
    UPDATE properties
    SET bedrooms          = COALESCE(%s, properties.bedrooms),
        bathrooms         = COALESCE(%s, properties.bathrooms),
        year_built        = COALESCE(%s, properties.year_built),
        lot_size_sqft     = COALESCE(%s, properties.lot_size_sqft),
        stories           = COALESCE(%s, properties.stories),
        property_style    = COALESCE(%s, properties.property_style),
        parking_garage    = COALESCE(%s, properties.parking_garage),
        hoa_fee           = COALESCE(%s, properties.hoa_fee),
        new_construction  = COALESCE(%s, properties.new_construction),
        list_price        = COALESCE(%s, properties.list_price),
        list_date         = COALESCE(%s, properties.list_date),
        listing_status    = COALESCE(%s, properties.listing_status),
        days_on_mls       = COALESCE(%s, properties.days_on_mls),
        estimated_value   = COALESCE(%s, properties.estimated_value),
        last_sold_price   = COALESCE(%s, properties.last_sold_price),
        last_sold_date    = COALESCE(%s, properties.last_sold_date),
        price_per_sqft    = COALESCE(%s, properties.price_per_sqft),
        updated_at        = NOW()
    FROM matched
    WHERE properties.id = matched.id
    RETURNING properties.id, properties.address;
"""


def row_to_params(row):
    """Convert a HomeHarvest row to UPSERT_SQL parameter list."""
    beds = safe_int(row.get("beds"))
    full_baths = safe_float(row.get("full_baths"))
    half_baths = safe_float(row.get("half_baths"))
    bathrooms = full_baths
    if half_baths:
        bathrooms = (bathrooms or 0) + 0.5 * half_baths
    if beds is None and bathrooms is None:
        return None
    return (
        normalize_address(row.get("formatted_address", "")),
        beds,
        bathrooms,
        safe_int(row.get("year_built")),
        safe_float(row.get("lot_sqft")),
        safe_int(row.get("stories")),
        safe_str(row.get("style")),
        safe_int(row.get("parking_garage")),
        safe_float(row.get("hoa_fee")),
        safe_bool(row.get("new_construction")),
        safe_float(row.get("list_price")),
        safe_date(row.get("list_date")),
        safe_str(row.get("status")),
        safe_int(row.get("days_on_mls")),
        safe_float(row.get("estimated_value")),
        safe_float(row.get("last_sold_price")),
        safe_date(row.get("last_sold_date")),
        safe_float(row.get("price_per_sqft")),
    )


def upsert_row(conn, row, dry_run=False):
    params = row_to_params(row)
    if params is None:
        return False
    address = row.get("formatted_address", "")
    beds = safe_int(row.get("beds"))
    bathrooms = params[2]
    if dry_run:
        log.info("[DRY] %s -> %s bed, %s bath", address, beds, bathrooms)
        return True
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, params)
        if cur.rowcount:
            conn.commit()
            log.info("  ✓ %s -> %s bed, %s bath", address, beds, bathrooms)
            return True
    return False


def try_fuzzy_match(conn, row, dry_run=False):
    params = row_to_params(row)
    if params is None:
        return False
    norm = params[0]
    m = re.match(r"(\d+)", norm)
    if not m:
        return False
    num = m.group(1)
    rest = norm[len(num):].strip()
    if dry_run:
        return True
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH matched AS (
                SELECT id, address FROM properties
                WHERE upper(address) LIKE %s
                AND (properties.bedrooms IS NULL OR properties.bathrooms IS NULL)
                LIMIT 1
            )
            UPDATE properties
            SET bedrooms          = COALESCE(%s, properties.bedrooms),
                bathrooms         = COALESCE(%s, properties.bathrooms),
                year_built        = COALESCE(%s, properties.year_built),
                lot_size_sqft     = COALESCE(%s, properties.lot_size_sqft),
                stories           = COALESCE(%s, properties.stories),
                property_style    = COALESCE(%s, properties.property_style),
                parking_garage    = COALESCE(%s, properties.parking_garage),
                hoa_fee           = COALESCE(%s, properties.hoa_fee),
                new_construction  = COALESCE(%s, properties.new_construction),
                list_price        = COALESCE(%s, properties.list_price),
                list_date         = COALESCE(%s, properties.list_date),
                listing_status    = COALESCE(%s, properties.listing_status),
                days_on_mls       = COALESCE(%s, properties.days_on_mls),
                estimated_value   = COALESCE(%s, properties.estimated_value),
                last_sold_price   = COALESCE(%s, properties.last_sold_price),
                last_sold_date    = COALESCE(%s, properties.last_sold_date),
                price_per_sqft    = COALESCE(%s, properties.price_per_sqft),
                updated_at        = NOW()
            FROM matched
            WHERE properties.id = matched.id
            RETURNING properties.id, properties.address;
            """,
            (f"{num}%{rest}%",) + params[1:],
        )
        if cur.rowcount:
            conn.commit()
            address = row.get("formatted_address", "")
            beds = safe_int(row.get("beds"))
            bathrooms = params[2]
            log.info("  ✓ (fuzzy) %s -> %s bed, %s bath", address, beds, bathrooms)
            return True
    return False


def scrape_year(location, year, dry_run=False):
    """Scrape sold listings for a single year and return (scraped, matched)."""
    date_from = f"{year}-01-01"
    date_to = f"{year}-12-31"
    log.info("Scraping %s sold listings for %s...", location, year)
    props = scrape_property(
        location=location,
        listing_type="SOLD",
        date_from=date_from,
        date_to=date_to,
        limit=10000,
    )
    if props.empty:
        log.info("  No sold listings for %s in %s", location, year)
        return 0, 0
    log.info("  Found %d sold listings", len(props))
    return len(props), 0


def scrape_and_upsert(conn, locations, start_year, end_year, dry_run, limit=0):
    results = {"scraped": 0, "matched": 0, "total": 0}
    for location in locations:
        for year in range(start_year, end_year + 1):
            scraped, _ = scrape_year(location, year, dry_run)
            results["scraped"] += scraped
            results["total"] += scraped
            if not dry_run:
                # Re-scrape to upsert (scrape_year only counts)
                date_from = f"{year}-01-01"
                date_to = f"{year}-12-31"
                props = scrape_property(
                    location=location,
                    listing_type="SOLD",
                    date_from=date_from,
                    date_to=date_to,
                    limit=10000,
                )
                for _, row in props.iterrows():
                    if limit and results["matched"] >= limit:
                        break
                    matched = upsert_row(conn, row, dry_run)
                    if not matched:
                        matched = try_fuzzy_match(conn, row, dry_run)
                    if matched:
                        results["matched"] += 1
    return results


def main():
    parser = argparse.ArgumentParser(description="Enrich properties with sold listing data")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max records to upsert")
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        stream=sys.stdout,
    )
    locations = ["Chattanooga, TN", "Hamilton County, TN"]
    conn = None
    if not args.dry_run:
        try:
            conn = psycopg2.connect(db_dsn())
            log.info("Connected to Postgres")
        except Exception as exc:
            log.error("Cannot connect to Postgres: %s", exc)
            sys.exit(1)
    try:
        results = scrape_and_upsert(
            conn, locations, args.start_year, args.end_year, args.dry_run, args.limit
        )
        log.info("Done! Scraped: %d, Matched: %d, Total listings: %d",
                 results["scraped"], results["matched"], results["total"])
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()