#!/usr/bin/env python3
"""
Hamilton County Foreclosure Scraper — Playwright Implementation
===============================================================

Scrapes foreclosure auction data from accessible sources and writes
to the foreclosures table in Postgres.

Data sources (attempted in order):
1. Hamilton County Clerk foreclosure notices
2. Tennessee legal notice publications
3. Derive from existing pre_foreclosure_flag + tax_delinquent properties

Usage:
  python3.12 foreclosure_scraper.py [--dry-run] [--limit 100]

Requires: pip install --break-system-packages playwright psycopg2-binary
          python3.12 -m playwright install chromium
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, date, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

from dotenv import load_dotenv
load_dotenv()

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

log = logging.getLogger(__name__)


async def derive_foreclosures_from_distress(db, dry_run=False):
    """
    Derive foreclosure records from properties that have pre_foreclosure_flag
    or are tax delinquent. This is a fallback when direct foreclosure sources
    are unavailable.

    In Tennessee, properties that are 1+ years tax delinquent are typically
    in the foreclosure pipeline. We create foreclosure records linked to
    existing properties.
    """
    query = """
        SELECT p.id, p.parcel_id, p.address, p.city, p.is_tax_delinquent,
               p.pre_foreclosure_flag, p.last_sale_date
        FROM properties p
        WHERE p.pre_foreclosure_flag = TRUE
           OR p.is_tax_delinquent = TRUE
        AND NOT EXISTS (
            SELECT 1 FROM foreclosures f WHERE f.property_id = p.id
        )
        ORDER BY p.is_tax_delinquent DESC, p.pre_foreclosure_flag DESC
        LIMIT %s
    """

    with db.cursor() as cur:
        cur.execute(query, (100,))
        rows = cur.fetchall()

    records = []
    for row in rows:
        prop_id, parcel_id, address, city, tax_delinquent, pre_foreclosure, last_sale = row

        # Determine status based on signals
        if pre_foreclosure:
            status = "active"
            source = "pre_foreclosure_flag"
        elif tax_delinquent:
            status = "at_risk"
            source = "tax_delinquent"
        else:
            continue

        # Estimate auction date (TN foreclosures typically 6-12 months after notice)
        auction_date = None
        if last_sale and last_sale > date(2000, 1, 1):
            auction_date = last_sale + timedelta(days=180)
        else:
            # Use 90-120 days from today for unknown/old sale dates
            today = date.today()
            auction_date = today + timedelta(days=90 + (hash(str(prop_id)) % 30))

        record = {
            "property_id": prop_id,
            "auction_date": auction_date,
            "auction_location": "Hamilton County Courthouse, Chattanooga, TN",
            "min_bid": None,
            "status": status,
            "source": source,
            "auction_url": None,
        }
        records.append(record)

    if not dry_run:
        insert_query = """
            INSERT INTO foreclosures (property_id, auction_date, auction_location, min_bid, status, source, auction_url)
            VALUES (%(property_id)s, %(auction_date)s, %(auction_location)s, %(min_bid)s, %(status)s, %(source)s, %(auction_url)s)
            ON CONFLICT DO NOTHING
        """
        with db.cursor() as cur:
            psycopg2.extras.execute_batch(cur, insert_query, records, page_size=50)
        db.commit()

    return records


async def scrape_hamilton_county_clerk(page: Page):
    """
    Attempt to scrape Hamilton County Clerk foreclosure notices.
    Falls back gracefully if the site is unreachable.
    """
    url = "https://www.hccountytn.gov/clerk/foreclosures"
    try:
        log.info(f"Attempting to scrape {url}")
        await page.goto(url, timeout=15000)
        content = await page.content()

        # Look for foreclosure listing patterns
        listings = []
        # Try common patterns
        for text in await page.query_selector_all('text'):
            if 'foreclosure' in text.lower() or 'auction' in text.lower():
                listings.append(text)

        return listings
    except Exception as e:
        log.warning(f"Could not scrape Hamilton County Clerk: {e}")
        return []


async def scrape_legal_notices(page: Page):
    """
    Attempt to scrape Tennessee legal notice publications for foreclosure notices.
    """
    sources = [
        "https://www.dailyheady.com",
        "https://tennesseelegalnotices.com",
    ]

    all_listings = []
    for url in sources:
        try:
            log.info(f"Attempting to scrape {url}")
            await page.goto(url, timeout=15000)
            content = await page.content()

            if 'foreclosure' in content.lower():
                log.info(f"Found foreclosure content at {url}")
                # Parse foreclosure notices
                # Implementation depends on actual site structure
                all_listings.append((url, content))
        except Exception as e:
            log.warning(f"Could not scrape {url}: {e}")

    return all_listings


async def main(dry_run=False, limit=100):
    """Main scraper entry point."""
    log.info("=" * 60)
    log.info("FORECLOSURE SCRAPER — Hamilton County, TN")
    log.info("=" * 60)
    log.warning(
        "DISABLED under the no-heuristic policy: this scraper's only working "
        "path was derive_foreclosures_from_distress(), which fabricated "
        "foreclosure records (invented auction dates/locations) from distress "
        "flags. No verified foreclosure data source is wired up yet. "
        "Candidate: foreclosure sale notices on the TFP legal-notices page "
        "(see probate_scraper.py for the working fetch pattern)."
    )
    return []

    db = None
    try:
        # Connect to DB
        db = psycopg2.connect(db_dsn())
        db.autocommit = False

        # Try direct scraping first
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            clerk_results = await scrape_hamilton_county_clerk(page)
            legal_results = await scrape_legal_notices(page)

            await browser.close()

        # If direct scraping yielded nothing, derive from existing distress data
        if not clerk_results and not legal_results:
            log.info("No direct foreclosure sources available. Deriving from distress data...")
            records = await derive_foreclosures_from_distress(db, dry_run=dry_run)
            log.info(f"Derived {len(records)} foreclosure records from distress data")
        else:
            # Parse and insert direct results
            # (implementation depends on actual site structure)
            records = []

        if dry_run:
            log.info(f"DRY RUN — would create {len(records)} foreclosure records")
            for r in records[:5]:
                log.info(f"  {r}")
        else:
            log.info(f"Created {len(records)} foreclosure records")

        return records

    except Exception as e:
        log.error(f"Foreclosure scraper failed: {e}")
        if db:
            db.rollback()
        raise
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Hamilton County Foreclosure Scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
