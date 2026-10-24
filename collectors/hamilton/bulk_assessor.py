#!/usr/bin/env python3
"""
Bulk Assessor Scraper — reads street list from OSM extraction
===============================================================

Usage:
  python3 bulk_assessor.py [--limit 1000] [--dry-run] [--start 0]
"""

import argparse, asyncio, json, logging, os, re, sys
from datetime import datetime
import psycopg2
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)
DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")
BASE_URL = "https://assessor.hamiltontn.gov/search"
STREETS_PATH = os.path.expanduser("~/proptoit/data/streets.json")
PROGRESS_FILE = os.path.expanduser("~/proptoit/data/bulk_progress.json")


def parse_number(text):
    if not text: return None
    c = re.sub(r"[^\d.]", "", str(text).strip())
    return float(c) if c else None

def parse_int(text):
    v = parse_number(text)
    return int(v) if v is not None else None

def parse_date(text):
    if not text or text.strip() in ("", "N/A", "None", "0"): return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try: return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError: continue
    return None

async def cell_text(row, i):
    cells = row.locator("div.tableLinks")
    if i >= await cells.count(): return ""
    return (await cells.nth(i).inner_text()).strip()

async def scrape_street(page, street):
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.locator('input[name="SearchStreetName"]').fill(street)
        await page.locator('input[value="Go"]').click()
        await page.wait_for_timeout(5000)
    except PlaywrightTimeout:
        log.warning("Timeout: %s", street)
        return []
    records = []
    while True:
        rows = page.locator("div.trRes")
        count = await rows.count()
        if count == 0: break
        for i in range(count):
            row = rows.nth(i)
            try:
                pid = await cell_text(row, 0)
                addr = await cell_text(row, 1)
                if not pid or not addr: continue
                owner = await cell_text(row, 2)
                yb = await cell_text(row, 3)
                av = await cell_text(row, 4)
                bsq = await cell_text(row, 5)
                sd = await cell_text(row, 7)
                sp = await cell_text(row, 8)
                pc = "37401"
                m = re.search(r"\b(3[67]\d{3})\b", addr)
                if m: pc = m.group(1)
                records.append({
                    "parcel_id": pid, "address": addr, "city": "Chattanooga",
                    "state": "TN", "postal_code": pc, "owner_of_record": owner or None,
                    "assessed_value": parse_number(av), "year_built": parse_int(yb),
                    "building_sqft": parse_number(bsq), "last_sale_price": parse_number(sp),
                    "last_sale_date": parse_date(sd),
                })
            except Exception: pass
        try:
            nb = page.locator("button:has-text('Next'), a:has-text('Next'), [aria-label='Next']").first
            if await nb.count() > 0 and await nb.is_enabled():
                await nb.click()
                await page.wait_for_timeout(3000)
            else: break
        except Exception: break
    return records

UPSERT = """
INSERT INTO properties (county_id,parcel_id,address,city,state,postal_code,owner_of_record,
    assessed_value,year_built,building_sqft,last_sale_price,last_sale_date,updated_at)
VALUES ('hamilton',%(parcel_id)s,%(address)s,%(city)s,%(state)s,%(postal_code)s,%(owner_of_record)s,
    %(assessed_value)s,%(year_built)s,%(building_sqft)s,%(last_sale_price)s,%(last_sale_date)s,NOW())
ON CONFLICT (county_id,parcel_id) DO UPDATE SET
    address=EXCLUDED.address,owner_of_record=EXCLUDED.owner_of_record,
    assessed_value=EXCLUDED.assessed_value,year_built=EXCLUDED.year_built,
    building_sqft=EXCLUDED.building_sqft,last_sale_price=EXCLUDED.last_sale_price,
    last_sale_date=EXCLUDED.last_sale_date,updated_at=NOW()
"""

def upsert(conn, data, dry):
    if not data.get("parcel_id") or not data.get("address"): return False
    row = {k: data.get(k) for k in ["parcel_id","address","city","state","postal_code",
        "owner_of_record","assessed_value","year_built","building_sqft",
        "last_sale_price","last_sale_date"]}
    if dry:
        log.info("[DRY] %s %s", row["parcel_id"], row["address"])
        return True
    with conn.cursor() as c: c.execute(UPSERT, row)
    conn.commit()
    return True

def save_progress(done, total):
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"done": done, "total": total, "at": datetime.now().isoformat()}, f)

async def run(streets, limit, dry, start, conn):
    total = 0
    done = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        to_do = streets[start:]
        log.info("Scraping %d streets (skip %d)", len(to_do), start)
        for idx, street in enumerate(to_do):
            if limit and total >= limit:
                log.info("Limit reached"); break
            num = start + idx + 1
            log.info("[%d/%d] %s", num, len(streets), street)
            recs = await scrape_street(page, street)
            log.info("  %d records", len(recs))
            for r in recs:
                upsert(conn, r, dry)
                total += 1
            done += 1
            if done % 25 == 0:
                save_progress(done, len(streets))
                log.info("  %d streets, %d records", done, total)
            await page.wait_for_timeout(500)
        await browser.close()
    save_progress(done, len(streets))
    return total

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--streets", default=STREETS_PATH)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
    log.info("Loading %s", a.streets)
    with open(a.streets) as f: data = json.load(f)
    streets = data.get("streets", data if isinstance(data, list) else [])
    log.info("%d streets", len(streets))
    conn = None
    if not a.dry_run:
        try:
            conn = psycopg2.connect(DB_DSN)
            log.info("Connected")
        except Exception as e:
            log.error("DB: %s", e); sys.exit(1)
    try:
        t = asyncio.run(run(streets, a.limit, a.dry_run, a.start, conn))
        log.info("Done: %d records", t)
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    main()
