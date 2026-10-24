#!/usr/bin/env python3
"""
Hamilton County Delinquent Tax Sale PDF Scraper

Downloads the annual delinquent tax sale list PDF, parses it with pdftotext,
extracts unpaid entries, matches them to properties in the database, and
upserts distress signals.

Usage:
  python3 tax_delinquent.py [--year 2026] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

import psycopg2
import psycopg2.extras

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, "/home/magicofelipe/proptoit")

DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")

BASE_URL = "https://www.hamiltontn.gov/Clerkmasterforms/taxsale/{year}TaxSale/DELINQUENT%20TAX%20SALE%20LIST%20{year}.pdf"


def download_pdf(year: int, dest: str) -> str:
    """Download the delinquent tax sale PDF for a given year."""
    url = BASE_URL.format(year=year)
    print(f"  Downloading: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response:
        with open(dest, "wb") as f:
            f.write(response.read())
    print(f"  Saved to: {dest} ({os.path.getsize(dest)} bytes)")
    return dest


def parse_pdf(pdf_path: str) -> str:
    """Convert PDF to text using pdftotext (-layout for column alignment)."""
    txt_path = pdf_path + ".txt"
    subprocess.run(
        ["pdftotext", "-layout", pdf_path, txt_path],
        check=True,
        capture_output=True,
    )
    with open(txt_path, "r") as f:
        return f.read()


def parse_entries(text: str, year: int) -> list:
    """Parse the pdftotext output and extract unpaid entries.

    The PDF has fixed-width columns (with -layout):
    [STATUS] DOCKET  ITEM#  PROPERTY#  ADDRESS  MAP_GROUP  PARCEL  AMOUNT

    Some entries have no PROPERTY# (e.g., GREENWOOD RD).
    """
    entries = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("DOCKET") or stripped.startswith("REVISED"):
            continue
        if stripped.startswith("PAID") or stripped.startswith("REMOVED"):
            continue

        # Use regex to parse the fixed-width layout
        # Pattern: spaces DOCKET ITEM# [PROPERTY#] ADDRESS MAP_GROUP PARCEL AMOUNT
        # The key insight: AMOUNT is always the last field ($X,XXX.XX)
        # PARCEL is before AMOUNT (e.g., 002.04 or 011 C158)
        # MAP_GROUP is 1-2 tokens before PARCEL
        # ADDRESS is variable (1+ tokens)
        # PROPERTY# is optional (numeric)
        # ITEM# and DOCKET are numeric

        # Strategy: find the amount from the right, then work backward
        parts = stripped.split()
        if len(parts) < 6:
            continue

        # Amount is last
        amount_str = parts[-1]

        # Parcel can be 1 or 2 tokens (e.g., "002.04" or "011 C158")
        # Map group is 1-2 tokens before parcel
        # We need to find where the address ends and map group begins.
        # Map group tokens look like: "145L", "K", "100A", "056"
        # They contain digits + optional letters, or are short alphanumeric.

        # Work from the right: amount, parcel, map_group, ...address, property#, item#, docket
        idx = len(parts) - 2  # start before amount

        # Extract parcel (1-2 tokens)
        parcel_tokens = []
        while idx >= 0 and len(parcel_tokens) < 2:
            token = parts[idx]
            # Parcel tokens look like: 002.04, 011, C158, 013
            if re.match(r'^\d+\.?\d*\s*[A-Z]?\d*$', token) or re.match(r'^[A-Z]\d+$', token):
                parcel_tokens.append(token)
                idx -= 1
            else:
                break

        if not parcel_tokens:
            continue

        parcel = " ".join(reversed(parcel_tokens))

        # Extract map group (1-2 tokens before parcel)
        map_group_tokens = []
        while idx >= 3 and len(map_group_tokens) < 3:
            token = parts[idx]
            # Map group tokens: digits + optional letter, or short alphanumeric
            # e.g., "145L", "K", "100A", "056", "137I"
            if re.match(r'^\d+[A-Z]$', token) or re.match(r'^[A-Z]$', token) or re.match(r'^\d+$', token):
                map_group_tokens.append(token)
                idx -= 1
            else:
                break

        if not map_group_tokens:
            continue

        map_group = " ".join(reversed(map_group_tokens))

        # Remaining tokens: DOCKET ITEM# [PROPERTY#] ADDRESS...
        left_parts = parts[:idx + 1]
        if len(left_parts) < 3:
            continue

        docket = left_parts[0]
        item_num = left_parts[1]

        # Check if third token is a property number (all digits)
        if re.match(r'^\d+$', left_parts[2]):
            property_num = left_parts[2]
            address = " ".join(left_parts[3:]) if len(left_parts) > 3 else ""
        else:
            property_num = ""
            address = " ".join(left_parts[2:])

        # Clean amount
        amount_clean = amount_str.replace("$", "").replace(",", "")
        try:
            amount_owed = float(amount_clean)
        except ValueError:
            amount_owed = 0.0

        # Build parcel_id as MAP GROUP + PARCEL
        parcel_id = f"{map_group} {parcel}".strip()

        entries.append({
            "docket": docket,
            "item_num": item_num,
            "property_num": property_num,
            "address": address.strip(),
            "map_group": map_group,
            "parcel": parcel,
            "parcel_id": parcel_id,
            "amount_owed": amount_owed,
            "year": year,
        })

    return entries


def match_and_upsert(conn, entries, year, dry_run=False):
    """Match entries to properties by address, then upsert distress signals."""
    count = 0

    # Build a set of unique addresses to query
    addr_to_entries = {}
    for entry in entries:
        addr = entry.get("address", "")
        if addr:
            addr_to_entries.setdefault(addr, []).append(entry)

    # Query properties for all addresses
    addresses = list(addr_to_entries.keys())
    if not addresses:
        return 0

    # Match by address ILIKE
    prop_map = {}
    for addr in addresses:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, parcel_id FROM properties
                   WHERE address ILIKE %s AND county_id = 'hamilton'
                   LIMIT 1""",
                (f"%{addr}%",),
            )
            row = cur.fetchone()
            if row:
                prop_map[addr] = row
    print(f"  Matched {len(prop_map)} unique addresses to {len(prop_map)} properties")

    # Upsert matched entries
    for entry in entries:
        addr = entry.get("address", "")
        if not addr or addr not in prop_map:
            continue

        prop = prop_map[addr]
        property_id = prop["id"]

        metadata = {
            "address": addr,
            "parcel_id": entry.get("parcel_id", ""),
            "amount_owed": entry.get("amount_owed", 0),
            "year": entry.get("year", year),
            "docket": entry.get("docket", ""),
        }

        if dry_run:
            print(f"  [DRY] {entry['parcel_id']}: {addr} — ${entry['amount_owed']:.2f}")
            count += 1
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO distress_signals
                        (property_id, signal_type, detected_at, source, severity, confidence, metadata)
                    VALUES
                        (%(property_id)s, 'tax_delinquent', NOW(),
                         'hamilton_county_tax_sale', 'high', 0.95, %(metadata)s::jsonb)
                    ON CONFLICT (property_id, signal_type)
                    DO UPDATE SET
                        detected_at = NOW(),
                        source = 'hamilton_county_tax_sale',
                        severity = 'high',
                        confidence = 0.95,
                        metadata = %(metadata)s::jsonb
                    """,
                    {
                        "property_id": property_id,
                        "metadata": json.dumps(metadata),
                    },
                )
            conn.commit()
            count += 1

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Hamilton County Delinquent Tax Sale PDF Scraper"
    )
    parser.add_argument("--year", type=int, default=2026, help="Tax sale year (default: 2026)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and match without writing to DB")
    args = parser.parse_args()

    print("=" * 60)
    print(f"HAMILTON COUNTY DELINQUENT TAX SALE SCRAPER — Year {args.year}")
    print("=" * 60)

    # Step 1: Download PDF
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name

    try:
        download_pdf(args.year, pdf_path)

        # Step 2: Parse with pdftotext
        print(f"\nParsing PDF...")
        text = parse_pdf(pdf_path)

        # Step 3: Extract unpaid entries
        print("Extracting entries...")
        entries = parse_entries(text, args.year)
        print(f"  Found {len(entries)} unpaid entries")

        if not entries:
            print("  No unpaid entries found. Exiting.")
            return

        # Step 4 & 5: Match to properties and upsert
        print(f"\nMatching to properties and upserting signals...")
        conn = psycopg2.connect(DB_DSN)
        try:
            n = match_and_upsert(conn, entries, args.year, dry_run=args.dry_run)
            if args.dry_run:
                print(f"  [DRY RUN] Would write {n} distress signals")
            else:
                print(f"  Wrote {n} distress signals")
        finally:
            conn.close()

    finally:
        # Cleanup temp files
        for path in [pdf_path, pdf_path + ".txt"]:
            if os.path.exists(path):
                os.remove(path)

    print("\nDone.")


if __name__ == "__main__":
    main()
