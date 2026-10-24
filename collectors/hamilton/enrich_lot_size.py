#!/usr/bin/env python3
"""
Lot Size Enrichment Pass — reads parcel_ids with NULL lot_size_sqft from Postgres,
visits each assessor detail page via Playwright, extracts acreage, and upserts.

Usage:
  python3 enrich_lot_size.py [--limit 50] [--dry-run]
"""

import os
import argparse
import asyncio
import logging
import re
import sys

import psycopg2
from playwright.async_api import async_playwright, Page

from dotenv import load_dotenv
load_dotenv()

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")

log = logging.getLogger(__name__)


def extract_lot_acres(text: str) -> float | None:
    """Extract acreage from land description text like '(1.9 ACRES)' or '0.45 ACRES'."""
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*ACRES?", text, re.IGNORECASE)
    if m:
        acres = float(m.group(1))
        return acres * 43560  # convert acres to sqft
    m = re.search(r"([\d,]+)\s*(?:SQ\.?\s*FT|SQUARE\s*FEET?|SQUARE\s*FOOT(?:S)?|SQFT)", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))
    # Fallback: "area of this property is (22700 SQUARE FOOT )"
    m = re.search(r"(\d[\d,]*)\s*SQUARE\s*FOOT", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


async def get_lot_size_from_detail(page: Page, parcel_id: str) -> float | None:
    """Navigate to assessor detail page and extract lot size in sqft."""
    try:
        encoded = parcel_id.replace(" ", "%20")
        await page.goto(
            f"https://assessor.hamiltontn.gov/card/{encoded}",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await page.wait_for_timeout(1500)
        body = await page.locator("body").inner_text()
        match = re.search(r"Land Description\s*\n\s*([^\n]*(?:\n[^\n]*)*)", body, re.IGNORECASE)
        if match:
            land_desc = match.group(1)
            return extract_lot_acres(land_desc)
        # Fallback: search entire body for acreage
        return extract_lot_acres(body)
    except Exception as exc:
        log.debug("Detail fetch failed for %s: %s", parcel_id, exc)
        return None


async def enrich(conn, streets: list[str] | None, limit: int, dry_run: bool) -> int:
    """Fetch parcel_ids with NULL lot_size, enrich from detail pages, upsert."""
    # Get parcels needing enrichment
    with conn.cursor() as cur:
        query = "SELECT parcel_id FROM properties WHERE lot_size_sqft IS NULL ORDER BY parcel_id"
        if limit:
            query += f" LIMIT {limit}"
        cur.execute(query)
        parcel_ids = [row[0] for row in cur.fetchall()]

    log.info("Found %d parcels with NULL lot_size", len(parcel_ids))
    if not parcel_ids:
        return 0

    updated = 0
    failed = 0

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

        for i, pid in enumerate(parcel_ids):
            sqft = await get_lot_size_from_detail(page, pid)
            if sqft is None:
                failed += 1
                if failed <= 5 or failed % 50 == 0:
                    log.info("No lot size for %s (total failed: %d)", pid, failed)
                continue

            if dry_run:
                log.info("[DRY] %s -> lot_size_sqft=%s", pid, sqft)
                updated += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE properties SET lot_size_sqft = %s, updated_at = NOW() WHERE parcel_id = %s",
                    (sqft, pid),
                )
            conn.commit()
            updated += 1
            if (i + 1) % 50 == 0:
                log.info("Progress: %d/%d processed (%d updated, %d failed)", i + 1, len(parcel_ids), updated, failed)

        await browser.close()

    log.info("Done. Updated: %d, Failed: %d", updated, failed)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich lot_size_sqft from assessor detail pages")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max parcels to process (0=all)")
    parser.add_argument("--streets", type=str, default="", help="Filter by street names (comma-separated)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        stream=sys.stdout,
    )

    conn = psycopg2.connect(DB_DSN)
    log.info("Connected to Postgres")

    try:
        enriched = asyncio.run(enrich(conn, None if not args.streets else [s.strip() for s in args.streets.split(",")], args.limit, args.dry_run))
        log.info("Enrichment complete. %d parcels updated.", enriched)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
