"""
Bradley County, TN assessor collector — bulk load from public ArcGIS FeatureServer.

Source: services1.arcgis.com/Ev0M319ggvf3XvKr/.../Parcel_Query/FeatureServer/0
(org: bradleygis.maps.arcgis.com — Bradley County GIS official)

One HTTP endpoint, no scraping. ~53k parcels. Upserts on (county_id, parcel_id).

Usage:
    python collectors/bradley/assessor.py --dry-run --limit 5
    python collectors/bradley/assessor.py            # full load
    python collectors/bradley/assessor.py --where "PropType LIKE '00%'"  # residential only
"""
import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

LAYER_URL = (
    "https://services1.arcgis.com/Ev0M319ggvf3XvKr/"
    "arcgis/rest/services/Parcel_Query/FeatureServer/0/query"
)
PAGE_SIZE = 1000
STATE = "TN"

log = logging.getLogger("bradley.assessor")


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


def polygon_centroid(rings) -> tuple | None:
    """Area-weighted centroid of the outer ring. Returns (lng, lat)."""
    if not rings:
        return None
    ring = rings[0]
    if not ring:
        return None
    if len(ring) < 4:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    a2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        cross = x1 * y2 - x2 * y1
        a2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a2) < 1e-12:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return (cx / (3 * a2), cy / (3 * a2))


def ms_date(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def clean(text) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split())


def classify_owner_type(owner_name: str) -> str:
    """individual | trust | company | government — mirrors Hamilton collector."""
    if not owner_name:
        return "unknown"
    n = owner_name.upper()
    gov = ["COUNTY OF", "CITY OF", "STATE OF", "HOUSING AUTH", "DEPT OF", "DEPARTMENT OF",
           "US GOV", "UNITED STATES", "TENNESSEE DEPT", "FEDERAL"]
    if any(g in n for g in gov):
        return "government"
    corp = ["LLC", "L L C", "INC", "CORP", "LP", "LTD", "COMPANY", "PROPERTIES",
            "PROPERTIES", "HOMES", "REALTY", "INVESTMENTS", "HOLDINGS", "GROUP",
            "PARTNERS", "ENTERPRISES", "ASSOCIATES", "TRUSTEE", "BANK", "CREDIT UNION",
            "CHURCH", "MINISTRIES", "FOUNDATION", "ASSOCIATION"]
    if any(c in n for c in corp):
        return "company"
    if "TRUST" in n:
        return "trust"
    return "individual"


def is_owner_occupant(situs_addr: str, mail_street: str, mail_city: str, mail_state: str) -> bool:
    if not situs_addr or not mail_street:
        return False
    if mail_state and mail_state.upper() not in (STATE, "TENNESSEE"):
        return False
    def norm(s: str) -> str:
        import re
        s = s.upper()
        s = re.sub(r"[.,#]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        # drop trailing city/county fragments
        for frag in (" BRADLEY CNTY", " BRADLEY COUNTY"):
            s = s.replace(frag, "")
        return s
    return norm(situs_addr) == norm(mail_street) or (
        norm(situs_addr).split(" ")[0:2] == norm(mail_street).split(" ")[0:2]
        and norm(situs_addr).split(" ")[-1] == norm(mail_street).split(" ")[-1]
    )


def build_mailing(a: dict) -> str:
    parts = [clean(a.get("MailAddr1")), clean(a.get("MailAddr2"))]
    apt = clean(a.get("MailAptNum"))
    line = " ".join(p for p in parts if p)
    if apt:
        line = f"{line} #{apt}"
    city = clean(a.get("MailCity")).title()
    state = clean(a.get("MailState"))
    zipc = clean(a.get("MailZip"))
    tail = " ".join(p for p in (city, state) if p)
    if zipc:
        tail = f"{tail} {zipc}" if tail else zipc
    if tail:
        line = f"{line}, {tail}"
    return line


def city_from(a: dict) -> str:
    """Situs city. CityCode like '000 - BRADLEY CNTY' → unincorporated."""
    code = clean(a.get("CityCode"))
    if code and not code.startswith("000"):
        return code.split(" - ", 1)[-1].title().replace("Cnty", "County")
    # fall back to LocCityCounty
    loc = clean(a.get("LocCityCounty"))
    if loc and "CNTY" not in loc.upper():
        return loc.title()
    return "Bradley County"


def map_record(a: dict) -> dict | None:
    parcel_id = clean(a.get("ParcelID")) or clean(a.get("GISKey"))
    if not parcel_id:
        return None
    situs = clean(a.get("FullAddr"))
    owner = clean(a.get("Owner1"))
    return {
        "parcel_id": parcel_id,
        "address": situs.title(),
        "city": city_from(a),
        "state": STATE,
        "postal_code": clean(a.get("LocationZip")) or None,
        "owner_of_record": owner.title() if owner else "",
        "owner_type": classify_owner_type(owner),
        "owner_mailing_address": build_mailing(a),
        "owner_occupant": is_owner_occupant(
            situs, clean(a.get("MailAddr1")), clean(a.get("MailCity")), clean(a.get("MailState"))
        ),
        "lot_size_sqft": (float(a["CALC_ACRE"]) * 43560) if a.get("CALC_ACRE") else None,
        "building_sqft": float(a["BldSqFt"]) if a.get("BldSqFt") else None,
        "year_built": a.get("YearBuilt") or None,
        "bedrooms": a.get("Beds") or None,
        "bathrooms": float(a["Baths"]) if a.get("Baths") else None,
        "last_sale_price": float(a["SalePrice"]) if a.get("SalePrice") else None,
        "last_sale_date": ms_date(a.get("SaleDate")),
        "assessed_value": float(a["AssessValue"]) if a.get("AssessValue") else None,
        "building_value": float(a["BldValue"]) if a.get("BldValue") else None,
        "land_value": float(a["LandValue"]) if a.get("LandValue") else None,
        "total_value": float(a["TotalValue"]) if a.get("TotalValue") else None,
        "property_category": clean(a.get("PropType")).split(" - ")[-1].title() or None,
        "property_type_code": clean(a.get("PropType")).split(" - ")[0] or None,
        "neighborhood": clean(a.get("SubdName")).split(" - ")[-1].title() or None,
        "zoning": clean(a.get("Zoning")) or None,
        "improved_status": clean(a.get("ImprovedStatus")),
    }


FETCH_FIELDS = ",".join([
    "ParcelID", "GISKey", "FullAddr", "LocCityCounty", "LocationZip", "CityCode",
    "Owner1", "Owner2", "Owner3", "MailAddr1", "MailAddr2", "MailAptNum",
    "MailCity", "MailState", "MailZip",
    "CALC_ACRE", "BldSqFt", "YearBuilt", "Beds", "Baths",
    "SalePrice", "SaleDate", "AssessValue", "BldValue", "LandValue", "TotalValue",
    "PropType", "SubdName", "Zoning", "ImprovedStatus", "OBJECTID",
])

INSERT_SQL = """
    INSERT INTO properties (
        county_id, parcel_id, address, city, state, postal_code,
        owner_of_record, owner_mailing_address, owner_occupant,
        lot_size_sqft, building_sqft, year_built, bedrooms, bathrooms,
        last_sale_price, last_sale_date,
        assessed_value, building_value, land_value, total_value,
        property_category, property_type_code, neighborhood, tax_district,
        lat, lng, deal_score, distress_score, created_at, updated_at
    ) VALUES (
        'bradley', %(parcel_id)s, %(address)s, %(city)s, %(state)s, %(postal_code)s,
        %(owner_of_record)s, %(owner_mailing_address)s, %(owner_occupant)s,
        %(lot_size_sqft)s, %(building_sqft)s, %(year_built)s, %(bedrooms)s, %(bathrooms)s,
        %(last_sale_price)s, %(last_sale_date)s,
        %(assessed_value)s, %(building_value)s, %(land_value)s, %(total_value)s,
        %(property_category)s, %(property_type_code)s, %(neighborhood)s, %(zoning)s,
        %(lat)s, %(lng)s, 0, 0, NOW(), NOW()
    )
    ON CONFLICT (county_id, parcel_id) DO UPDATE SET
        address = EXCLUDED.address,
        city = EXCLUDED.city,
        postal_code = EXCLUDED.postal_code,
        owner_of_record = EXCLUDED.owner_of_record,
        owner_mailing_address = EXCLUDED.owner_mailing_address,
        owner_occupant = EXCLUDED.owner_occupant,
        lot_size_sqft = COALESCE(EXCLUDED.lot_size_sqft, properties.lot_size_sqft),
        building_sqft = COALESCE(EXCLUDED.building_sqft, properties.building_sqft),
        year_built = COALESCE(EXCLUDED.year_built, properties.year_built),
        bedrooms = COALESCE(EXCLUDED.bedrooms, properties.bedrooms),
        bathrooms = COALESCE(EXCLUDED.bathrooms, properties.bathrooms),
        last_sale_price = COALESCE(EXCLUDED.last_sale_price, properties.last_sale_price),
        last_sale_date = COALESCE(EXCLUDED.last_sale_date, properties.last_sale_date),
        assessed_value = COALESCE(EXCLUDED.assessed_value, properties.assessed_value),
        building_value = COALESCE(EXCLUDED.building_value, properties.building_value),
        land_value = COALESCE(EXCLUDED.land_value, properties.land_value),
        total_value = COALESCE(EXCLUDED.total_value, properties.total_value),
        property_category = COALESCE(EXCLUDED.property_category, properties.property_category),
        neighborhood = COALESCE(EXCLUDED.neighborhood, properties.neighborhood),
        updated_at = NOW()
"""


def fetch_all(where: str = "1=1"):
    """Paginate through all features, following ESRI's exceededTransferLimit."""
    offset = 0
    total = 0
    while True:
        params = {
            "where": where,
            "outFields": FETCH_FIELDS,
            "returnGeometry": "true",
            "outSR": "4326",
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


def run(dry_run: bool, where: str, limit: int = 0):
    conn = psycopg2.connect(db_dsn())
    stats = {"fetched": 0, "mapped": 0, "upserted": 0, "skipped_no_parcel": 0, "skipped_no_geom": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM properties WHERE county_id='bradley'")
            log.info("Existing bradley rows: %d", cur.fetchone()[0])

        batch = []
        for feats in fetch_all(where):
            for f in feats:
                stats["fetched"] += 1
                rec = map_record(f.get("attributes") or {})
                if not rec:
                    stats["skipped_no_parcel"] += 1
                    continue
                geom = (f.get("geometry") or {}).get("rings")
                c = polygon_centroid(geom) if geom else None
                if c and -90 < c[1] < 90:
                    rec["lng"], rec["lat"] = round(c[0], 6), round(c[1], 6)
                else:
                    rec["lng"] = rec["lat"] = None
                    stats["skipped_no_geom"] += 1
                stats["mapped"] += 1
                batch.append(rec)

            if dry_run:
                continue
            if len(batch) >= 1000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, INSERT_SQL, batch, page_size=200)
                conn.commit()
                stats["upserted"] += len(batch)
                log.info("  Upserted %d...", stats["upserted"])
                batch = []

        if not dry_run and batch:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, INSERT_SQL, batch, page_size=200)
            conn.commit()
            stats["upserted"] += len(batch)

        if dry_run and batch:
            s = batch[0]
            log.info("Sample: %s | %s | %s | occ=%s | sale=%s %s | assessed=%s",
                     s["address"], s["parcel_id"], s["owner_of_record"],
                     s["owner_occupant"], s["last_sale_price"], s["last_sale_date"],
                     s["assessed_value"])
        if limit and dry_run:
            log.info("NOTE: limit=%d ignored for dry-run (stats cover full fetch)", limit)
    finally:
        conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Bradley County assessor bulk load")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--where", default="1=1", help="ArcGIS where clause")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        stream=sys.stdout)

    stats = run(args.dry_run, args.where, args.limit)
    print(f"\n{'='*50}")
    print("Results:")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
