#!/usr/bin/env python3
"""
Bulk Skip Trace Loader
======================
Loads mailing addresses from Hamilton County's bulk assessor CSV export
into skip_trace_results table, matching on property address.

Also updates properties.owner_mailing_address and owner_occupant via COALESCE upsert.

Usage:
  python3 load_skip_trace_bulk.py [--csv PATH] [--dry-run] [--limit N]

The CSV is downloaded from:
  https://www.hamiltontn.gov/_downloadsAssessor/AssessorExportCSV.zip
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime

import psycopg2
import psycopg2.extras

CSV_URL = "https://www.hamiltontn.gov/_downloadsAssessor/AssessorExportCSV.zip"
LAYOUT_URL = "https://www.hamiltontn.gov/_downloadsAssessor/AssessorExtractLayout.pdf"
DB_DSN = "host=localhost port=5433 dbname=proptoit user=postgres"

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Owner type classification
# ---------------------------------------------------------------------------

OWNER_TYPE_PATTERNS = [
    ("government", [r"\bSTATE\s+OF\b", r"\bCOUNTY\s+OF\b", r"\bCITY\s+OF\b",
                     r"\bFEDERAL\b", r"\bMUNICIPAL\b", r"\bBOARD\s+OF\b"]),
    ("corporation", [r"\bINC\b", r"\bCORP\b", r"\bCORPORATION\b", r"\bCO\b",
                      r"\bCOMPANY\b", r"\bLTD\b"]),
    ("llc", [r"\bLLC\b", r"\bLLP\b", r"\bLP\b", r"\bL\.L\.C\b"]),
    ("trust", [r"\bTRUST\b", r"\bREVOCABLE\b", r"\bIRREVOCABLE\b",
                r"\bTRUSTEE\b", r"\bTRUST\s+FOR\b"]),
    ("estate", [r"\bESTATE\b", r"\bEST\b", r"\bEXECUTOR\b", r"\bADMINISTRATOR\b",
                 r"\bHEIR\b", r"\bPROBATE\b"]),
    ("partnership", [r"\bPARTNERSHIP\b", r"\b&\s+\w", r"\bAND\b", r"\bASSOC\b"]),
]


def classify_owner_type(owner_name: str) -> str:
    """Classify owner name into a type category."""
    if not owner_name:
        return "unknown"
    
    name_upper = owner_name.upper().strip()
    
    for otype, patterns in OWNER_TYPE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, name_upper):
                return otype
    
    # Check for "&" pattern (joint ownership = individual)
    if "&" in name_upper:
        return "individual"
    
    return "individual"


def normalize_owner_name(raw: str) -> str:
    """Normalize owner name for deduplication."""
    if not raw:
        return ""
    # Uppercase, collapse whitespace, remove trailing punctuation
    name = raw.upper().strip()
    name = re.sub(r"[^\w\s&.,']", "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(".,")
    return name


def build_mailing_string(street: str, unit: str, line2: str, 
                         city: str, state: str, zipcode: str) -> str:
    """Build a formatted mailing address string."""
    parts = [street, unit, line2]
    street_parts = [p.strip() for p in parts if p.strip()]
    
    if not street_parts and city:
        # If no street, use city as fallback (shouldn't happen with 99.86% complete)
        return f"{city}, {state} {zipcode}".strip(", ")
    
    street_str = " ".join(street_parts)
    return f"{street_str}, {city}, {state} {zipcode}".strip(", ")


def is_owner_occupant(prop_address: str, prop_zip: str,
                       mail_street: str, mail_zip: str) -> bool:
    """Determine if owner lives at the property address."""
    if not mail_street or not mail_zip:
        return False
    
    # PO Box → not occupant
    if re.match(r"P\.?\s*O\.?\s*BOX", mail_street.upper()):
        return False
    # C/O → not occupant (care of someone else)
    if re.match(r"C\.?\s*O\.", mail_street.upper()):
        return False
    # Different zip → not occupant
    if prop_zip and mail_zip != prop_zip:
        return False
    
    # Compare street number + first word of street name
    def leading(s: str):
        m = re.match(r"(\d+)\s+(\w+)", s.upper().strip())
        return (m.group(1), m.group(2)) if m else (None, None)
    
    pn, ps = leading(prop_address)
    mn, ms = leading(mail_street)
    
    if pn and mn and pn == mn and ps == ms:
        return True
    
    return False


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

SKIP_TRACE_UPSERT = """
    INSERT INTO skip_trace_results (
        property_id, owner_name_raw, owner_name_normalized, owner_type,
        mailing_street, mailing_city, mailing_state, mailing_zip,
        phone_number, email, source, source_url, raw_data,
        is_current, is_verified, scraped_at, updated_at
    ) VALUES (
        %(property_id)s, %(owner_name_raw)s, %(owner_name_normalized)s, %(owner_type)s,
        %(mailing_street)s, %(mailing_city)s, %(mailing_state)s, %(mailing_zip)s,
        %(phone_number)s, %(email)s, %(source)s, %(source_url)s, %(raw_data)s,
        true, false, NOW(), NOW()
    )
    ON CONFLICT (property_id) WHERE is_current = true
    DO UPDATE SET
        owner_name_raw       = EXCLUDED.owner_name_raw,
        owner_name_normalized = EXCLUDED.owner_name_normalized,
        owner_type           = EXCLUDED.owner_type,
        mailing_street       = EXCLUDED.mailing_street,
        mailing_city         = EXCLUDED.mailing_city,
        mailing_state        = EXCLUDED.mailing_state,
        mailing_zip          = EXCLUDED.mailing_zip,
        source               = EXCLUDED.source,
        source_url           = EXCLUDED.source_url,
        raw_data             = EXCLUDED.raw_data,
        scraped_at         = NOW(),
        updated_at         = NOW()
"""

PROPERTY_UPSERT = """
    UPDATE properties SET
        owner_mailing_address = EXCLUDED.mailing_address,
        owner_occupant        = EXCLUDED.owner_occupant,
        updated_at            = NOW()
    WHERE id = EXCLUDED.property_id
      AND (owner_mailing_address IS NULL 
           OR EXCLUDED.mailing_address > properties.owner_mailing_address)
"""

# We'll use executemany for skip_trace and a separate update for properties
# Actually, let's do this in two steps for clarity

PROPERTY_UPDATE = """
    UPDATE properties SET
        owner_mailing_address = %(mailing_address)s,
        owner_occupant        = %(owner_occupant)s,
        updated_at            = NOW()
    WHERE id = %(property_id)s
      AND (owner_mailing_address IS NULL 
           OR owner_mailing_address = '')
"""


def load_csv_addresses(csv_path: str) -> list[dict]:
    """Parse the bulk CSV and return records with mailing data."""
    records = []
    
    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        
        # Find column indices
        col_map = {col: i for i, col in enumerate(header)}
        
        for row in reader:
            try:
                owner = col_map.get('OWNER_NAME_1', -1)
                addr = col_map.get('ST_ADDRESS', -1)
                mail = col_map.get('MAIL_ST_NAME', -1)
                unit = col_map.get('MAIL_UNIT', -1)
                line2 = col_map.get('MAIL_LINE_2', -1)
                city = col_map.get('MAIL_CITY', -1)
                state = col_map.get('MAIL_STATE', -1)
                zipcode = col_map.get('MAIL_ZIP', -1)
                
                owner_name = row[owner].strip() if owner < len(row) else ''
                prop_addr = row[addr].strip() if addr < len(row) else ''
                
                if not prop_addr or not owner_name:
                    continue
                
                mail_street = row[mail].strip() if mail < len(row) else ''
                mail_unit = row[unit].strip() if unit < len(row) else ''
                mail_line2 = row[line2].strip() if line2 < len(row) else ''
                mail_city = row[city].strip() if city < len(row) else ''
                mail_state = row[state].strip() if state < len(row) else ''
                mail_zip = row[zipcode].strip() if zipcode < len(row) else ''
                
                # Skip if no meaningful mailing data
                if not mail_street and not mail_city:
                    continue
                
                records.append({
                    'prop_addr': prop_addr.upper(),
                    'owner_name': owner_name,
                    'mail_street': mail_street,
                    'mail_unit': mail_unit,
                    'mail_line2': mail_line2,
                    'mail_city': mail_city,
                    'mail_state': mail_state,
                    'mail_zip': mail_zip,
                })
            except Exception as e:
                log.debug("Row parse error: %s", e)
    
    return records


def run(csv_path: str, dry_run: bool, limit: int = 0) -> dict:
    """Load CSV data into skip_trace_results and update properties."""
    
    log.info("Loading CSV: %s", csv_path)
    records = load_csv_addresses(csv_path)
    log.info("Parsed %d records with mailing data", len(records))
    
    if limit:
        records = records[:limit]
        log.info("Limiting to %d records", limit)
    
    conn = psycopg2.connect(DB_DSN)
    
    try:
        # Step 1: Build address → property_id lookup
        log.info("Building address lookup from properties table...")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, upper(trim(address)) as addr_upper, postal_code
                FROM properties
                WHERE address != ''
            """)
            addr_to_prop = {row['addr_upper']: {
                'id': row['id'],
                'postal_code': row['postal_code']
            } for row in cur.fetchall()}
        
        log.info("Loaded %d property addresses", len(addr_to_prop))
        
        # Step 2: Match CSV records to properties
        matched = 0
        unmatched = 0
        skip_trace_records = []
        property_updates = []
        
        for rec in records:
            prop_addr = rec['prop_addr']
            
            if prop_addr not in addr_to_prop:
                unmatched += 1
                continue
            
            matched += 1
            prop_info = addr_to_prop[prop_addr]
            prop_id = prop_info['id']
            prop_zip = prop_info['postal_code']
            
            # Build mailing address string
            mailing_str = build_mailing_string(
                rec['mail_street'], rec['mail_unit'], rec['mail_line2'],
                rec['mail_city'], rec['mail_state'], rec['mail_zip']
            )
            
            # Classify owner
            owner_type = classify_owner_type(rec['owner_name'])
            owner_normalized = normalize_owner_name(rec['owner_name'])
            
            # Determine occupant status
            occupant = is_owner_occupant(prop_addr, prop_zip,
                                         rec['mail_street'], rec['mail_zip'])
            
            # Build raw data JSON
            raw_data = {
                'owner_name': rec['owner_name'],
                'mailing_address': mailing_str,
                'mail_street': rec['mail_street'],
                'mail_unit': rec['mail_unit'],
                'mail_line2': rec['mail_line2'],
                'mail_city': rec['mail_city'],
                'mail_state': rec['mail_state'],
                'mail_zip': rec['mail_zip'],
                'prop_address': prop_addr,
            }
            
            skip_trace_records.append({
                'property_id': prop_id,
                'owner_name_raw': rec['owner_name'],
                'owner_name_normalized': owner_normalized,
                'owner_type': owner_type,
                'mailing_street': f"{rec['mail_street']} {rec['mail_unit']} {rec['mail_line2']}".strip(),
                'mailing_city': rec['mail_city'],
                'mailing_state': rec['mail_state'],
                'mailing_zip': rec['mail_zip'],
                'phone_number': None,
                'email': None,
                'source': 'assessor_bulk',
                'source_url': CSV_URL,
                'raw_data': json.dumps(raw_data),
            })
            
            property_updates.append({
                'property_id': prop_id,
                'mailing_address': mailing_str,
                'owner_occupant': occupant,
            })
        
        log.info("Matched: %d, Unmatched: %d", matched, unmatched)
        
        if dry_run:
            log.info("DRY RUN — skipping DB writes")
            log.info("Would insert %d skip_trace_results", len(skip_trace_records))
            log.info("Would update %d properties", len(property_updates))
            
            # Show sample
            if skip_trace_records:
                sample = skip_trace_records[0]
                log.info("Sample: %s -> %s, %s %s %s",
                    sample['owner_name_raw'],
                    sample['mailing_street'],
                    sample['mailing_city'],
                    sample['mailing_state'],
                    sample['mailing_zip'])
            
            return {'matched': matched, 'unmatched': unmatched, 'dry_run': True}
        
        # Step 3: Insert skip trace results
        log.info("Inserting %d skip trace records...", len(skip_trace_records))
        with conn.cursor() as cur:
            cur.executemany(SKIP_TRACE_UPSERT, skip_trace_records)
        conn.commit()
        log.info("Inserted %d skip trace records", len(skip_trace_records))
        
        # Step 4: Update properties (only if no mailing address yet)
        log.info("Updating %d properties...", len(property_updates))
        with conn.cursor() as cur:
            cur.executemany(PROPERTY_UPDATE, property_updates)
        conn.commit()
        log.info("Updated %d properties", len(property_updates))
        
        return {
            'matched': matched,
            'unmatched': unmatched,
            'skip_trace_inserted': len(skip_trace_records),
            'properties_updated': len(property_updates),
        }
    
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_csv() -> str:
    """Download the assessor CSV zip and extract."""
    import urllib.request
    
    log.info("Downloading %s", CSV_URL)
    
    zip_path = '/tmp/AssessorExportCSV.zip'
    if os.path.exists(zip_path) and os.path.getsize(zip_path) > 10_000_000:
        log.info("Using existing CSV file (%d bytes)", os.path.getsize(zip_path))
    else:
        urllib.request.urlretrieve(CSV_URL, zip_path)
        log.info("Downloaded %d bytes", os.path.getsize(zip_path))
    
    # Extract
    csv_path = '/tmp/AssessorExport.csv'
    if not os.path.exists(csv_path):
        log.info("Extracting CSV...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall('/tmp/')
    
    return csv_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Load bulk skip trace data")
    parser.add_argument("--csv", type=str, default=None,
                        help="Path to CSV file (downloads if not specified)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit to N records")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        stream=sys.stdout)
    
    if args.csv:
        csv_path = args.csv
    else:
        csv_path = download_csv()
    
    result = run(csv_path, args.dry_run, args.limit)
    
    print(f"\n{'='*50}")
    print(f"Results:")
    print(f"  Matched:     {result.get('matched', 0):,}")
    print(f"  Unmatched:   {result.get('unmatched', 0):,}")
    if not args.dry_run:
        print(f"  Skip traces: {result.get('skip_trace_inserted', 0):,}")
        print(f"  Properties updated: {result.get('properties_updated', 0):,}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
