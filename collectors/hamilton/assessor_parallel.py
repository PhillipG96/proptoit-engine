"""
Hamilton County Assessor scraper — parallelized with checkpoint resume.
Splits streets across N browser contexts for concurrent scraping.
Writes last_processed_street to ~/.hermes/cron/output/assessor_checkpoint.json
so a restart picks up where it left off.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

from dotenv import load_dotenv
load_dotenv()

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")
BASE_URL = "https://assessor.hamiltontn.gov/search"
CHECKPOINT_FILE = Path.home() / ".hermes" / "cron" / "output" / "assessor_parallel_checkpoint.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg, *args):
    logging.info(msg, *args)

def parse_number(text):
    if not text:
        return None
    cleaned = re.sub(r'[^\d.\-]', '', text.strip())
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

def parse_int(text):
    val = parse_number(text)
    return int(val) if val is not None else None

def parse_date(text):
    if not text or text.strip() in ("", "N/A", "None", "0", "00/00/0000"):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def extract_lot_acres(text: str):
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*ACRES?", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 43560
    m = re.search(r"([\d,]+)\s*(?:SQ\.?\s*FT|SQUARE\s*FEET|SQFT)", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))
    return None

async def cell_text(row, index: int) -> str:
    cells = row.locator("div.tableLinks")
    count = await cells.count()
    if index >= count:
        return ""
    return (await cells.nth(index).inner_text()).strip()

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

UPSERT_SQL = """
    INSERT INTO properties (
        county_id, parcel_id, address, city, state, postal_code,
        owner_of_record, owner_mailing_address, owner_occupant,
        assessed_value, year_built, building_sqft, lot_size_sqft, bedrooms, bathrooms,
        last_sale_price, last_sale_date, updated_at
    ) VALUES (
        'hamilton', %(parcel_id)s, %(address)s, %(city)s, %(state)s, %(postal_code)s,
        %(owner_of_record)s, %(owner_mailing_address)s, %(owner_occupant)s,
        %(assessed_value)s, %(year_built)s, %(building_sqft)s, %(lot_size_sqft)s,
        %(bedrooms)s, %(bathrooms)s,
        %(last_sale_price)s, %(last_sale_date)s,
        NOW()
    )
    ON CONFLICT (county_id, parcel_id) DO UPDATE SET
        address               = EXCLUDED.address,
        owner_of_record       = EXCLUDED.owner_of_record,
        assessed_value        = EXCLUDED.assessed_value,
        year_built            = EXCLUDED.year_built,
        building_sqft         = EXCLUDED.building_sqft,
        lot_size_sqft         = COALESCE(EXCLUDED.lot_size_sqft, properties.lot_size_sqft),
        last_sale_price       = EXCLUDED.last_sale_price,
        last_sale_date        = EXCLUDED.last_sale_date,
        owner_mailing_address = COALESCE(EXCLUDED.owner_mailing_address, properties.owner_mailing_address),
        owner_occupant        = CASE WHEN EXCLUDED.owner_mailing_address IS NOT NULL
                                     THEN EXCLUDED.owner_occupant
                                     ELSE properties.owner_occupant END,
        updated_at            = NOW()
"""

def upsert_batch(conn, records):
    """Upsert a batch of records. Returns count of new/updated."""
    try:
        with conn.cursor() as cur:
            for rec in records:
                row = {
                    "parcel_id": str(rec["parcel_id"]).strip(),
                    "address": str(rec["address"]).strip(),
                    "city": rec.get("city", "Chattanooga"),
                    "state": rec.get("state", "TN"),
                    "postal_code": rec.get("postal_code") or "37401",
                    "owner_of_record": rec.get("owner_of_record"),
                    "owner_mailing_address": rec.get("owner_mailing_address"),
                    "owner_occupant": bool(rec.get("owner_occupant", False)),
                    "assessed_value": rec.get("assessed_value"),
                    "year_built": rec.get("year_built"),
                    "building_sqft": rec.get("building_sqft"),
                    "lot_size_sqft": rec.get("lot_size_sqft"),
                    "bedrooms": rec.get("bedrooms"),
                    "bathrooms": rec.get("bathrooms"),
                    "last_sale_price": rec.get("last_sale_price"),
                    "last_sale_date": rec.get("last_sale_date"),
                }
                cur.execute(UPSERT_SQL, row)
            conn.commit()
    except Exception:
        # Without an explicit rollback the connection stays in aborted
        # state and every later statement on it fails too.
        conn.rollback()
        raise
    return len(records)

# ---------------------------------------------------------------------------
# Scrape one street (needs own page)
# ---------------------------------------------------------------------------

async def scrape_street(page: Page, street: str) -> list:
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.locator('input[name="SearchStreetName"]').fill(street)
        await page.locator('input[value="Go"]').click()
        await page.wait_for_timeout(5000)
    except PlaywrightTimeout:
        # None = transient failure (retry candidate). [] = genuinely empty
        # street. The caller used to mark BOTH as permanently completed,
        # silently dropping every parcel on a street that timed out once.
        _log("Timeout loading search page for '%s'", street)
        return None

    records = []
    page_num = 0

    while True:
        page_num += 1
        rows = page.locator("div.trRes")
        count = await rows.count()
        _log("  Page %d: %d rows", page_num, count)

        if count == 0:
            break

        for i in range(count):
            row = rows.nth(i)
            try:
                parcel_id  = await cell_text(row, 0)
                address    = await cell_text(row, 1)
                owner      = await cell_text(row, 2)
                year_built = await cell_text(row, 3)
                assessed   = await cell_text(row, 4)
                bldg_sqft  = await cell_text(row, 5)
                sale_date  = await cell_text(row, 7)
                sale_price = await cell_text(row, 8)

                if not parcel_id or not address:
                    continue

                postal_code = "37401"
                m = re.search(r"\b(3[67]\d{3})\b", address)
                if m:
                    postal_code = m.group(1)

                records.append({
                    "parcel_id": parcel_id,
                    "address": address,
                    "city": "Chattanooga",
                    "state": "TN",
                    "postal_code": postal_code,
                    "owner_of_record": owner or None,
                    "owner_mailing_address": None,
                    "owner_occupant": False,
                    "assessed_value": parse_number(assessed),
                    "year_built": parse_int(year_built),
                    "building_sqft": parse_number(bldg_sqft),
                    "lot_size_sqft": None,
                    "bedrooms": None,
                    "bathrooms": None,
                    "last_sale_price": parse_number(sale_price),
                    "last_sale_date": parse_date(sale_date),
                })
            except Exception as exc:
                _log("Row %d parse error: %s", i, exc)

        next_btn = page.locator("button:has-text('Next'), a:has-text('Next'), [aria-label='Next']").first
        try:
            if await next_btn.count() > 0 and await next_btn.is_enabled():
                await next_btn.click()
                await page.wait_for_timeout(3000)
            else:
                break
        except Exception:
            break

    return records

# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
# Parallel workers each finish streets out of order, so a single
# "last_street" value was overwritten by whichever worker wrote last and
# resume silently skipped work it hadn't done. Completed streets are now
# appended to a shared .jsonl and resume filters against the full set.

CHECKPOINT_DONE_FILE = CHECKPOINT_FILE.with_suffix(".completed.jsonl")

def record_completed(street: str):
    CHECKPOINT_DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_DONE_FILE.open("a") as f:
        f.write(street + "\n")
        f.flush()
        os.fsync(f.fileno())

def load_completed() -> set:
    """Streets recorded as completed by any worker in a previous run."""
    done = set()
    if CHECKPOINT_DONE_FILE.exists():
        try:
            for line in CHECKPOINT_DONE_FILE.read_text().splitlines():
                if line.strip():
                    done.add(line.strip())
        except Exception:
            pass
    return done

def load_checkpoint() -> str:
    """Return the last processed street, or '' if none (legacy format)."""
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return data.get("last_street", "")
        except Exception:
            pass
    return ""

def save_checkpoint(street: str):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps({"last_street": street, "updated_at": datetime.utcnow().isoformat()}))

# ---------------------------------------------------------------------------
# Worker: one browser context, one DB connection, a slice of streets
# ---------------------------------------------------------------------------

async def worker(context_idx: int, streets_slice: list[str], no_enrich: bool = False) -> int:
    """Scrape a slice of streets. Returns total records scraped."""
    # Each worker gets its own DB connection
    conn = psycopg2.connect(DB_DSN)
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(20000)

        total = 0
        failed_streets = []
        for street in streets_slice:
            _log("[%d] Street: %s", context_idx, street)
            records = await scrape_street(page, street)
            if records is None:
                _log("[%d] '%s' transient failure — NOT marking completed", context_idx, street)
                failed_streets.append(street)
                continue
            _log("[%d] Extracted %d records", context_idx, len(records))

            if records:
                upsert_batch(conn, records)
                total += len(records)
            record_completed(street)
            save_checkpoint(street)
            _log("[%d] Upserted %d, checkpoint saved", context_idx, len(records))

        # One retry pass for transient failures before giving up
        for street in failed_streets:
            records = await scrape_street(page, street)
            if records is None:
                _log("[%d] '%s' failed retry — will resume next run", context_idx, street)
                continue
            if records:
                upsert_batch(conn, records)
                total += len(records)
            record_completed(street)
            save_checkpoint(street)

            # Small delay between streets to avoid hammering the server
            await page.wait_for_timeout(500)

        await browser.close()
    conn.close()

    return total

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def scrape_parallel(streets: list[str], num_workers: int = 4, no_enrich: bool = False) -> int:
    """Run multiple browser contexts in parallel, each processing a street slice."""
    checkpoint = load_checkpoint()
    completed = load_completed()
    _log("Checkpoint: last_street='%s', %d streets recorded complete", checkpoint, len(completed))

    # Prefer the per-street completed set (accurate under parallel workers);
    # fall back to legacy last_street only when the set is empty.
    if completed:
        remaining = [s for s in streets if s not in completed]
        _log("Total streets: %d, already done: %d, remaining: %d",
             len(streets), len(streets) - len(remaining), len(remaining))
    else:
        start_idx = 0
        if checkpoint:
            try:
                start_idx = streets.index(checkpoint) + 1
                _log("Resuming from index %d (after '%s')", start_idx, checkpoint)
            except ValueError:
                _log("Checkpoint street '%s' not found in street list, starting from beginning", checkpoint)
                start_idx = 0
        remaining = streets[start_idx:]
        _log("Total streets: %d, already done: %d, remaining: %d", len(streets), start_idx, len(remaining))

    if not remaining:
        _log("All streets already scraped!")
        return 0

    # Split streets across workers
    slices = []
    for i in range(num_workers):
        start = i * len(remaining) // num_workers
        end = (i + 1) * len(remaining) // num_workers if i < num_workers - 1 else len(remaining)
        slices.append(remaining[start:end])

    _log("Workers: %d, slices: %s", num_workers, [len(s) for s in slices])

    # Connect to DB (one connection per worker, created inside worker)
    # Workers create their own connections inside async context

    results = await asyncio.gather(
        *(worker(i, s, no_enrich=no_enrich) for i, s in enumerate(slices)),
        return_exceptions=True,
    )

    total = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            _log("Worker %d failed: %s", i, r)
        else:
            total += r

    return total

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hamilton County Assessor scraper (parallel)")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel browser contexts")
    parser.add_argument("--no-enrich", action="store_true", help="Skip enrichment (fast mode)")
    parser.add_argument("--streets", type=str, default="", help="Comma-separated street names")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", stream=sys.stdout)

    # Load expanded street list from JSON (6,875 streets)
    import json
    from pathlib import Path
    street_file = Path(__file__).resolve().parent.parent.parent / "data" / "streets_filtered.json"
    with open(street_file) as f:
        data = json.load(f)
    streets = data["streets"]

    if args.streets:
        streets = [s.strip().upper() for s in args.streets.split(",") if s.strip()]

    _log("Streets to scan: %d, workers: %d", len(streets), args.workers)

    total = asyncio.run(scrape_parallel(streets, num_workers=args.workers, no_enrich=args.no_enrich))
    _log("Done. Total records scraped: %d", total)

if __name__ == "__main__":
    main()
