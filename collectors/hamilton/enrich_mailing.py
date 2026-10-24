#!/usr/bin/env python3
"""
Mailing Address Enrichment
==========================
Visits each property's detail page on the Hamilton County Assessor site
and back-fills owner_mailing_address + owner_occupant for all properties
that are missing a mailing address.

Usage:
  python3.12 enrich_mailing.py [--limit N] [--dry-run]
"""

import os
import argparse
import asyncio
import logging
import re
import sys

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
BASE_URL = "https://assessor.hamiltontn.gov/search"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def fetch_missing(conn, limit: int) -> list[dict]:
    sql = """
        SELECT id, parcel_id, address, postal_code
        FROM properties
        WHERE owner_mailing_address IS NULL
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {limit}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def update_mailing(conn, db_id: int, mailing: str, occupant: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE properties
               SET owner_mailing_address = %s,
                   owner_occupant        = %s,
                   updated_at            = NOW()
               WHERE id = %s""",
            (mailing, occupant, db_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Parse the .mailing-address table
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


async def extract_mailing(page: Page) -> dict | None:
    """Return dict with street/city/state/zip, or None if not found."""
    table = page.locator(".mailing-address").first
    if not await table.count():
        return None

    text = _clean(await table.inner_text())

    # Build a label→value map from the table cells
    cells = await page.locator(".mailing-address td").all_inner_texts()
    cells = [_clean(c) for c in cells if _clean(c)]

    data: dict[str, str] = {}
    key = None
    for cell in cells:
        if cell in ("Owner", "Address", "City", "State", "Zip"):
            key = cell
        elif key:
            data[key] = cell
            key = None

    if not data.get("Address"):
        return None

    return data


def build_mailing_string(d: dict) -> str:
    addr    = d.get("Address", "").strip()
    city    = d.get("City", "").strip()
    state   = d.get("State", "TN").strip()
    zip_    = d.get("Zip", "").strip()
    return f"{addr}, {city}, {state} {zip_}".strip(", ")


def is_owner_occupant(prop_address: str, prop_zip: str, mailing: dict) -> bool:
    """Heuristic: mailing address matches property address → owner-occupied."""
    mail_zip = mailing.get("Zip", "").strip()
    mail_addr = mailing.get("Address", "").upper().strip()
    prop_addr = prop_address.upper().strip()

    # PO Box → not occupant
    if re.match(r"P\.?\s*O\.?\s*BOX", mail_addr):
        return False
    # Different zip → not occupant
    if mail_zip and prop_zip and mail_zip != prop_zip:
        return False
    # Compare street number + first word of street name
    def leading(s: str):
        m = re.match(r"(\d+)\s+(\w+)", s)
        return (m.group(1), m.group(2)) if m else (None, None)

    pn, ps = leading(prop_addr)
    mn, ms = leading(mail_addr)
    if pn and mn and pn == mn and ps == ms:
        return True
    return False


# ---------------------------------------------------------------------------
# Browser logic — search by parcel ID and open detail
# ---------------------------------------------------------------------------

async def get_mailing_for_parcel(page: Page, parcel_id: str) -> dict | None:
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)

        # Search by parcel ID
        await page.locator('input[name="SearchParcel"]').fill(parcel_id)
        await page.locator('input[value="Go"]').click()
        await page.wait_for_timeout(4000)

        rows = page.locator("div.trRes")
        count = await rows.count()
        if count == 0:
            log.debug("No results for parcel %s", parcel_id)
            return None

        # Click first (should be the only) result
        await rows.first.click()
        await page.wait_for_timeout(2500)

        return await extract_mailing(page)

    except PlaywrightTimeout:
        log.warning("Timeout on parcel %s", parcel_id)
        return None
    except Exception as exc:
        log.warning("Error on parcel %s: %s", parcel_id, exc)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(props: list[dict], conn, dry_run: bool) -> int:
    updated = 0

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

        for i, prop in enumerate(props, 1):
            parcel_id = prop["parcel_id"]
            log.info("[%d/%d] %s — %s", i, len(props), parcel_id, prop["address"])

            mailing = await get_mailing_for_parcel(page, parcel_id)
            if not mailing:
                log.warning("  No mailing address found")
                continue

            mailing_str = build_mailing_string(mailing)
            occupant    = is_owner_occupant(prop["address"], prop["postal_code"], mailing)

            log.info("  → %s  [occupant=%s]", mailing_str, occupant)

            if not dry_run:
                update_mailing(conn, prop["id"], mailing_str, occupant)
            updated += 1

        await browser.close()

    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",   type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", stream=sys.stdout)

    conn = psycopg2.connect(db_dsn())
    props = fetch_missing(conn, args.limit)
    log.info("Properties needing mailing address: %d", len(props))

    if not props:
        log.info("Nothing to do.")
        conn.close()
        return

    updated = asyncio.run(run(props, conn if not args.dry_run else None, args.dry_run))
    log.info("Done. Updated: %d", updated)
    conn.close()


if __name__ == "__main__":
    main()
