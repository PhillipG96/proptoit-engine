#!/usr/bin/env python3
"""
Probate Scraper — Chattanooga Times Free Press Legal Notices

Scrapes NOTICE TO CREDITORS probate notices from the TFP classifieds
page, extracts decedent names, and cross-references them against
property owner_of_record to create high-confidence probate signals.

Each notice contains:
  - Decedent name (e.g. "DOUGLAS M. WHITAKER, SR.")
  - Case number (e.g. "26-P-475")
  - Filing date (e.g. "June 15, 2026")
  - Death date (e.g. "May 29, 2026")
  - Personal representative (from full notice text)

Usage:
  python3 probate_scraper.py [--dry-run]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from collectors.base import normalize_record, save_cache, load_cache, get_scraped_at

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TFP_LEGAL_NOTICES_URL = (
    "https://www.timesfreepress.com/classifieds/category/legal-notices/"
)

CACHE_PATH = "/tmp/proptoit/probate_notices.cache"

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

# Regex patterns
# "NOTICE TO CREDITORS  ESTATE OF  DOUGLAS M. WHITAKER, SR. DECEDENT NO. 26-P-475"
CREDITOR_NOTICE_RE = re.compile(
    r"NOTICE\s+TO\s+CREDITORS\s+"
    r"ESTATE\s+OF\s+"
    r"([A-Z][A-Z\s\.,\-]{2,60})"  # decedent name
    r"DECEDENT\s+NO\.\s+"
    r"([A-Z0-9\-]{3,20})",         # case number
    re.I,
)

# Filing date: "on  June 15, 2026, Letters Testamentary were issued"
FILING_DATE_RE = re.compile(
    r"on\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.I,
)

# Death date: "died  May 29, 2026"
DEATH_DATE_RE = re.compile(
    r"died\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.I,
)

# Personal representative: "issued to the undersigned ... [NAME]"
# We'll grab the full notice text and extract what we can.
PERSONAL_REP_RE = re.compile(
    r"issued\s+(?:to|upon)\s+(?:the\s+)?(?:undersigned\s+)?([A-Z][A-Z\s\.,\-]{2,40})",
    re.I,
)


def fetch_page(url):
    """Fetch a webpage with retries."""
    import urllib.request
    import time

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PropToIt-Probate/1.0)"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == 2:
                print(f"  Failed {url}: {e}")
                return None
            time.sleep(2)
    return None


def parse_probate_notices(html):
    """Extract all NOTICE TO CREDITORS probate notices from HTML."""
    notices = []
    seen_case_numbers = set()

    # Find all NOTICE TO CREDITORS blocks
    for m in CREDITOR_NOTICE_RE.finditer(html):
        decedent_name = m.group(1).strip()
        case_number = m.group(2).strip()

        # Deduplicate by case number (HTML renders each notice twice)
        if case_number in seen_case_numbers:
            continue
        seen_case_numbers.add(case_number)

        # Get a window of text around this match for additional info
        start = max(0, m.start() - 200)
        end = min(len(html), m.end() + 500)
        context = html[start:end]

        # Filing date
        filing_match = FILING_DATE_RE.search(context)
        filing_date = None
        if filing_match:
            filing_date = filing_match.group(1).strip()

        # Death date
        death_match = DEATH_DATE_RE.search(context)
        death_date = None
        if death_match:
            death_date = death_match.group(1).strip()

        # Personal representative (best effort)
        rep_match = PERSONAL_REP_RE.search(context)
        personal_rep = None
        if rep_match:
            personal_rep = rep_match.group(1).strip()

        notices.append(
            {
                "decedent_name": decedent_name,
                "case_number": case_number,
                "filing_date": filing_date,
                "death_date": death_date,
                "personal_rep": personal_rep,
                "raw_context": context[:1000],  # truncated for storage
            }
        )

    return notices


def clean_name_for_match(name):
    """Normalize a name for database matching."""
    # Remove suffixes
    cleaned = re.sub(r",?\s*(SR|JR|III|IV|II|MD|DDS)\.?", "", name, flags=re.I)
    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.upper()

def find_owner_matches(conn, decedent_name):
    """Find properties where owner_of_record matches the decedent name.
    
    Uses a two-tier approach:
    1. Full name match (exact or close) -> high confidence (0.95)
    2. First + Last name match (reversed order) -> medium confidence (0.7)
    
    Filters out matches where only a common first name matches (e.g., "JO" matching "JOHNNY").
    """
    cleaned = clean_name_for_match(decedent_name)

    # Split into name parts for flexible matching
    parts = cleaned.split()
    if not parts:
        return []

    # Extract first and last name components
    first_name = parts[0]
    last_name = parts[-1]

    records = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Strategy 1: Full name LIKE (handles "JANET E PICKARD" in owner string)
        # Strategy 2: First + Last name parts (handles "PICKARD JANET E" reversed order)
        cur.execute(
            """
            SELECT p.id, p.parcel_id, p.address, p.owner_of_record,
                   p.last_sale_date, p.assessed_value,
                   CASE 
                       WHEN UPPER(p.owner_of_record)::text LIKE UPPER(%s) THEN 'full_name'
                       ELSE 'first_last'
                   END AS match_type
            FROM properties p
            WHERE p.county_id = 'hamilton'
            AND p.owner_of_record IS NOT NULL
            AND (
                UPPER(p.owner_of_record)::text LIKE UPPER(%s)
                OR (
                    UPPER(p.owner_of_record)::text LIKE UPPER('%%' || %s || '%%')
                    AND UPPER(p.owner_of_record)::text LIKE UPPER('%%' || %s || '%%')
                )
            )
            ORDER BY p.assessed_value DESC
            LIMIT 10
            """,
            (f"%{cleaned}%", f"%{cleaned}%", first_name, last_name),
        )

        for row in cur.fetchall():
            owner = row["owner_of_record"]
            match_type = row["match_type"]
            
            # Post-filter: for first_last matches, verify the first name is a whole word
            # not a substring (e.g., "JO" shouldn't match "JOHNNY")
            if match_type == 'first_last':
                # Check that first_name appears as a whole word in the owner string
                owner_words = re.split(r'[^A-Z0-9]', owner.upper())
                if first_name not in owner_words:
                    continue  # "JO" is not a whole word in "JOHNNY"
            
            record = dict(row)
            record["match_type"] = match_type
            # Full name matches get high confidence, first+last get medium
            record["confidence"] = 0.95 if match_type == 'full_name' else 0.7
            records.append(record)

    return records


def upsert_probate_notices(conn, notices):
    """Save probate notices to the database."""
    count = 0
    with conn.cursor() as cur:
        for notice in notices:
            cur.execute(
                """
                INSERT INTO probate_notices (
                    case_number, decedent_name, filing_date, death_date,
                    personal_rep, raw_context, scraped_at
                ) VALUES (%(case_number)s, %(decedent_name)s, %(filing_date)s,
                            %(death_date)s, %(personal_rep)s, %(raw_context)s, NOW())
                ON CONFLICT (case_number) DO UPDATE SET
                    decedent_name = EXCLUDED.decedent_name,
                    filing_date = EXCLUDED.filing_date,
                    death_date = EXCLUDED.death_date,
                    personal_rep = EXCLUDED.personal_rep,
                    raw_context = EXCLUDED.raw_context,
                    scraped_at = NOW()
                """,
                {
                    "case_number": notice["case_number"],
                    "decedent_name": notice["decedent_name"],
                    "filing_date": notice["filing_date"],
                    "death_date": notice["death_date"],
                    "personal_rep": notice["personal_rep"],
                    "raw_context": notice["raw_context"],
                },
            )
            count += 1
    conn.commit()
    return count


def upsert_probate_signals(conn, matches):
    """Create distress signals for matched properties."""
    count = 0
    with conn.cursor() as cur:
        for match in matches:
            cur.execute(
                """
                INSERT INTO distress_signals (
                    property_id, signal_type, detected_at, source,
                    severity, confidence, metadata
                ) VALUES (
                    %(property_id)s, 'probate', NOW(),
                    'probate_notice', 'high', %(confidence)s, %(metadata)s::jsonb
                )
                ON CONFLICT (property_id, signal_type) DO UPDATE SET
                    detected_at = NOW(),
                    source = EXCLUDED.source,
                    severity = EXCLUDED.severity,
                    confidence = EXCLUDED.confidence,
                    metadata = EXCLUDED.metadata
                """,
                {
                    "property_id": match["id"],
                    "confidence": match.get("confidence", 0.95),
                    "metadata": json.dumps(
                        {
                            "case_number": match["case_number"],
                            "decedent_name": match["decedent_name"],
                            "owner_of_record": match["owner_of_record"],
                            "filing_date": match.get("filing_date"),
                            "death_date": match.get("death_date"),
                            "match_type": match.get("match_type", "unknown"),
                        }
                    ),
                },
            )
            count += 1
    conn.commit()
    return count


def run(dry_run=False):
    """Run the probate scraper."""
    print("=" * 60)
    print("PROBATE SCRAPER — Chattanooga Times Free Press")
    print("=" * 60)

    # Fetch the page
    print("\n>>> Fetching legal notices page...")
    html = fetch_page(TFP_LEGAL_NOTICES_URL)
    if not html:
        print("  Failed to fetch page. Aborting.")
        return

    print(f"  Fetched {len(html)} bytes")

    # Parse notices
    print("\n>>> Parsing probate notices...")
    notices = parse_probate_notices(html)
    print(f"  Found {len(notices)} probate notices")

    if not notices:
        print("  No probate notices found. Done.")
        return

    # Print notice summary
    print("\n  Notices found:")
    for n in notices:
        print(
            f"    {n['decedent_name']} | {n['case_number']} | "
            f"Filed: {n['filing_date'] or 'N/A'} | Died: {n['death_date'] or 'N/A'}"
        )

    # Connect to DB
    conn = psycopg2.connect(db_dsn())
    try:
        # Save notices to DB
        print("\n>>> Saving probate notices to database...")
        if not dry_run:
            saved = upsert_probate_notices(conn, notices)
            print(f"  Saved {saved} notices")
        else:
            print(f"  [DRY RUN] Would save {len(notices)} notices")

        # Match against property owners
        print("\n>>> Matching decedents to property owners...")
        all_matches = []
        for notice in notices:
            matches = find_owner_matches(conn, notice["decedent_name"])
            for m in matches:
                enriched = {
                    **m,
                    "case_number": notice["case_number"],
                    "decedent_name": notice["decedent_name"],
                    "filing_date": notice["filing_date"],
                    "death_date": notice["death_date"],
                }
                all_matches.append(enriched)

        print(f"  Found {len(all_matches)} property matches")
        for m in all_matches:
            print(
                f"    {m['decedent_name']} -> {m['parcel_id']} | "
                f"{m['address']} | Owner: {m['owner_of_record']}"
            )

        # Create distress signals
        if all_matches:
            print("\n>>> Creating probate distress signals...")
            if not dry_run:
                signal_count = upsert_probate_signals(conn, all_matches)
                print(f"  Created {signal_count} signals")
            else:
                print(f"  [DRY RUN] Would create {len(all_matches)} signals")

        # Save cache
        if not dry_run:
            save_cache(CACHE_PATH, notices)
            print(f"\n  Cache saved to {CACHE_PATH}")

        print(f"\n✅ Done: {len(notices)} notices, {len(all_matches)} matches")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probate Scraper")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
