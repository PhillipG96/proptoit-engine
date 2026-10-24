#!/usr/bin/env python3
"""
Hamilton County Tax Sale Collector — Weekly Monitor
=====================================================

Downloads the delinquent tax sale PDF, tracks revisions, and:
1. Updates distress signals for active tax delinquent properties
2. Populates the foreclosures table with auction data
3. Only updates when the PDF has been revised (checks revision date)
4. Handles PAID/REMOVED status properly

Source: https://www.hamiltontn.gov/Clerkmasterforms/taxsale/{year}TaxSale/DELINQUENT%20TAX%20SALE%20LIST%20{year}.pdf
"""

import re
import json
import sys
import logging
import argparse
import subprocess
import tempfile
from datetime import datetime, date
from pathlib import Path

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

# PDF URL pattern — matches the Clerk's naming convention
PDF_URL = "https://www.hamiltontn.gov/Clerkmasterforms/taxsale/{year}TaxSale/DELINQUENT%20TAX%20SALE%20LIST%20{year}.pdf"

# DB connection — use trust auth on localhost:5433
DB_DSN = "postgresql://postgres@localhost:5433/proptoit"


def download_pdf(url: str) -> bytes:
    """Download a PDF, raise on non-200."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Convert PDF to text using pdftotext CLI."""
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(pdf_bytes)
        pdf_path = f.name
    
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', pdf_path, '-'],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def extract_revision_date(text: str) -> str | None:
    """Extract the revision date from the PDF header."""
    m = re.search(r'REVISED\s+(\d{1,2}/\d{1,2}/\d{4})', text)
    return m.group(1) if m else None


def extract_auction_date(text: str) -> date | None:
    """Extract the auction date from the PDF.
    
    The info PDF has the auction date. The main PDF has the revision date.
    We need to check the info PDF separately, or hardcode based on year.
    """
    # For now, return None — we'll get it from the info PDF
    return None


def extract_entries(text: str) -> list[dict]:
    """Parse the tax sale entries from the PDF text.
    
    Format:
    PAID   11255     1738       1415         PARK AVE                    145L  K       002.04      $14,757.42
       11255     2530                    GREENWOOD RD                137I  E        018         $2,188.52
    REMOVED 11256   9871           PERSIMMON LN      137O   A    009      $3,195.96
    
    Status is determined by the prefix: PAID, REMOVED, or blank (active).
    """
    entries = []
    lines = text.split('\n')
    
    # Find the header row
    header_idx = None
    for i, line in enumerate(lines):
        if 'DOCKET' in line.upper() and 'ADDRESS' in line.upper():
            header_idx = i
            break
    
    if header_idx is None:
        log.warning("No table header found in PDF")
        return entries
    
    # Parse each data line
    for i in range(header_idx + 1, len(lines)):
        line = lines[i].strip()
        
        if not line:
            continue
        
        # Determine status from prefix
        status = 'active'
        check_line = line.upper()
        if check_line.startswith('PAID'):
            status = 'paid'
            line = line[4:].strip()  # Remove PAID prefix
        elif check_line.startswith('REMOVED'):
            status = 'removed'
            line = line[7:].strip()  # Remove REMOVED prefix
        
        # Parse the entry
        # Format: DOCKET# ITEM# PROPERTY#(optional) ADDRESS MAPGROUP PARCEL MINBID
        # Try to extract address and property number
        prop_match = re.search(r'(\d{2}-\d{4,5}-\d{2})', line)
        # Address: number + street name + suffix (normalize whitespace)
        # Try to extract address and property number
        prop_match = re.search(r'(\d{2}-\d{4,5}-\d{2})', line)
        # Address: number + street name + suffix
        address_match = re.search(
            r'(\d+\s+[A-Z]+\s+(?:ST|AVE|LN|DR|CT|BLVD|PKWY|PIKE|CIR|PL|RD|WAY|TRAIL|DRIVE|BOULEVARD))',
            line, re.IGNORECASE
        )
        address = re.sub(r'\s+', ' ', address_match.group(1).strip()) if address_match else None
        # Min bid: dollar amount at end
        min_bid_match = re.search(r'\$(\d{1,3}(?:,\d{3})*\.\d{2})\s*$', line)
        
        if address or prop_match:
            entry = {
                'address': address,
                'property_num': prop_match.group(1) if prop_match else None,
                'min_bid': float(min_bid_match.group(1).replace(',', '')) if min_bid_match else None,
                'status': status,
            }
            entries.append(entry)
    
    return entries


def match_address_to_property(conn, address: str) -> str | None:
    """Find property_id by address (fuzzy match)."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id FROM properties
        WHERE address ILIKE %s
        LIMIT 1
    """, (f"%{address.strip()}%",))
    row = cur.fetchone()
    cur.close()
    return row['id'] if row else None


def match_property_number(conn, property_num: str) -> str | None:
    """Find property_id by property number."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id FROM properties
        WHERE property_number = %s
        LIMIT 1
    """, (property_num.strip(),))
    row = cur.fetchone()
    cur.close()
    return row['id'] if row else None


def update_distress_signals(conn, prop_map: dict) -> dict:
    """Update distress signals for tax delinquent properties."""
    stats = {'added': 0, 'updated': 0, 'removed': 0}
    
    cur = conn.cursor()
    
    # Get current active property IDs
    active_ids = list(prop_map.keys())
    
    # Remove signals for properties no longer on the list
    if active_ids:
        placeholders = ','.join(['%s'] * len(active_ids))
        cur.execute(f"""
            DELETE FROM distress_signals
            WHERE signal_type = 'tax_delinquent'
            AND source = 'hamilton_county_tax_sale'
            AND property_id NOT IN ({placeholders})
        """, active_ids)
        stats['removed'] = cur.rowcount
    
    # Upsert signals for active properties
    for prop_id, entry in prop_map.items():
        if entry['status'] != 'active':
            continue
        
        metadata = {
            'source': 'hamilton_county_tax_sale',
            'min_bid': entry.get('min_bid'),
            'property_num': entry.get('property_num'),
        }
        
        cur.execute("""
            INSERT INTO distress_signals (property_id, signal_type, source, severity, confidence, metadata)
            VALUES (%s, 'tax_delinquent', 'hamilton_county_tax_sale', 'high', 0.95, %s::jsonb)
            ON CONFLICT (property_id, signal_type, source)
            DO UPDATE SET
                detected_at = NOW(),
                severity = 'high',
                confidence = 0.95,
                metadata = %s::jsonb
            RETURNING id
        """, (prop_id, json.dumps(metadata), json.dumps(metadata)))
        
        stats['updated'] += 1
    
    cur.close()
    return stats


def update_foreclosures(conn, prop_map: dict, auction_date: date | None) -> dict:
    """Update foreclosures table with tax sale auction data."""
    stats = {'upserted': 0, 'paid': 0, 'removed': 0}
    
    cur = conn.cursor()
    
    for prop_id, entry in prop_map.items():
        if entry['status'] == 'active':
            cur.execute("""
                INSERT INTO foreclosures (property_id, auction_date, status, min_bid, source, auction_location)
                VALUES (%s, %s, 'upcoming', %s, 'hamilton_county_tax_sale', 'Hamilton County Courthouse')
                ON CONFLICT (property_id, source)
                DO UPDATE SET
                    auction_date = EXCLUDED.auction_date,
                    min_bid = EXCLUDED.min_bid,
                    status = 'upcoming',
                    recorded_at = NOW()
            """, (prop_id, auction_date, entry.get('min_bid')))
            stats['upserted'] += 1
        
        elif entry['status'] == 'paid':
            cur.execute("""
                INSERT INTO foreclosures (property_id, auction_date, status, min_bid, source, auction_location)
                VALUES (%s, %s, 'cancelled', %s, 'hamilton_county_tax_sale', 'Hamilton County Courthouse')
                ON CONFLICT (property_id, source)
                DO UPDATE SET
                    status = 'cancelled',
                    recorded_at = NOW()
            """, (prop_id, auction_date, entry.get('min_bid')))
            stats['paid'] += 1
        
        elif entry['status'] == 'removed':
            cur.execute("""
                INSERT INTO foreclosures (property_id, auction_date, status, source, auction_location)
                VALUES (%s, %s, 'cancelled', 'hamilton_county_tax_sale', 'Hamilton County Courthouse')
                ON CONFLICT (property_id, source)
                DO UPDATE SET
                    status = 'cancelled',
                    recorded_at = NOW()
            """, (prop_id, auction_date))
            stats['removed'] += 1
    
    cur.close()
    return stats


def main():
    print("DISABLED: tax_sale_collector.py's signal path deletes and re-derives"
          "\ntax_delinquent signals from the annual tax-sale PDF. The verified"
          "\nsource is now scripts/parse_ctrudelq.py (county bulk file, Wed 2am cron).")
    return
    parser = argparse.ArgumentParser(description="Hamilton County Tax Sale Collector")
    parser.add_argument('--year', type=int, default=datetime.now().year, help="Tax sale year")
    parser.add_argument('--dry-run', action='store_true', help="Parse and display without writing to DB")
    args = parser.parse_args()
    
    log.info(f"Hamilton County Tax Sale Collector — Year {args.year}")
    
    # Download PDFs
    log.info("Downloading tax sale list...")
    pdf_bytes = download_pdf(PDF_URL.format(year=args.year))
    
    # Parse
    log.info("Parsing PDF...")
    text = pdf_to_text(pdf_bytes)
    
    revision_date = extract_revision_date(text)
    auction_date = extract_auction_date(text)
    
    log.info(f"  Revision date: {revision_date}")
    log.info(f"  Auction date: {auction_date}")
    
    # Extract entries
    log.info("Extracting entries...")
    entries = extract_entries(text)
    
    active = [e for e in entries if e['status'] == 'active']
    paid = [e for e in entries if e['status'] == 'paid']
    removed = [e for e in entries if e['status'] == 'removed']
    
    log.info(f"  Active: {len(active)}, Paid: {len(paid)}, Removed: {len(removed)}")
    
    if args.dry_run:
        log.info("DRY RUN — not writing to DB")
        for e in entries[:5]:
            log.info(f"  {e}")
        return
    
    # Connect to DB
    log.info("Connecting to database...")
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    
    try:
        # Match entries to properties
        log.info("Matching to properties...")
        prop_map = {}  # property_id -> entry
        for entry in entries:
            prop_id = None
            if entry.get('property_num'):
                prop_id = match_property_number(conn, entry['property_num'])
            if not prop_id and entry.get('address'):
                prop_id = match_address_to_property(conn, entry['address'])
            if prop_id:
                prop_map[prop_id] = entry
        
        log.info(f"  Matched {len(prop_map)} addresses")
        
        # Update distress signals
        log.info("Updating distress signals...")
        signal_stats = update_distress_signals(conn, prop_map)
        log.info(f"  Signals: {signal_stats}")
        
        # Update foreclosures table
        log.info("Updating foreclosures table...")
        fc_stats = update_foreclosures(conn, prop_map, auction_date)
        log.info(f"  Foreclosures: {fc_stats}")
        
        conn.commit()
        log.info("Done.")
    
    except Exception:
        conn.rollback()
        raise


if __name__ == '__main__':
    main()
