#!/usr/bin/env python3
"""
Bulk New Property Loader
=========================
Imports missing Hamilton County properties from the bulk assessor CSV
into the properties table. Only loads TN properties in local cities
that aren't already in our DB.

Usage:
  python3 load_new_properties_bulk.py [--csv PATH] [--dry-run] [--limit N]
"""

import argparse
import csv
import logging
import os
import re
import sys
from datetime import datetime

import psycopg2

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

LOCAL_ZIPS = {
    '37401', '37402', '37403', '37404', '37405', '37406', '37407', '37408', '37409',
    '37410', '37411', '37412', '37415', '37416', '37421', '37422', '37449', '37453',
    '37466', '37467', '37469', '37484', '37485', '37490', '37492', '37494',
    '37303', '37315', '37321', '37338', '37343', '37355', '37369', '37373', '37377', '37379',
}

ZIP_TO_CITY = {
    '37401': 'Chattanooga', '37402': 'Chattanooga', '37403': 'Chattanooga',
    '37404': 'Chattanooga', '37405': 'Chattanooga', '37406': 'Chattanooga',
    '37407': 'Chattanooga', '37408': 'Chattanooga', '37409': 'Hixson',
    '37410': 'Chattanooga', '37411': 'Chattanooga', '37412': 'Chattanooga',
    '37415': 'Chattanooga', '37416': 'Chattanooga', '37421': 'Chattanooga',
    '37422': 'Chattanooga', '37449': 'Chattanooga', '37453': 'Chattanooga',
    '37466': 'Chattanooga', '37467': 'Chattanooga', '37469': 'Chattanooga',
    '37484': 'Chattanooga', '37485': 'Chattanooga', '37490': 'Chattanooga',
    '37492': 'Chattanooga', '37494': 'Chattanooga',
    '37303': 'Ooltewah', '37315': 'Collegedale', '37321': 'Dayton',
    '37338': 'Graysville', '37343': 'Hixson', '37355': 'Ooltewah',
    '37369': 'Apison', '37373': 'Sale Creek', '37377': 'Signal Mountain',
    '37379': 'Soddy Daisy',
}


def parse_number(text):
    if not text:
        return None
    cleaned = re.sub(r'[^\d.\-]', '', text.strip())
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_date(text):
    if not text or text.strip() in ('', 'N/A', 'None', '0', '00/00/0000'):
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(text.strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def classify_owner_type(owner_name):
    if not owner_name:
        return 'individual'
    name_upper = owner_name.upper().strip()
    patterns = [
        ('government', [r'\bSTATE\s+OF\b', r'\bCOUNTY\s+OF\b', r'\bCITY\s+OF\b',
                        r'\bFEDERAL\b', r'\bMUNICIPAL\b']),
        ('corporation', [r'\bINC\b', r'\bCORP\b', r'\bCORPORATION\b', r'\bCO\b',
                         r'\bCOMPANY\b', r'\bLTD\b']),
        ('llc', [r'\bLLC\b', r'\bLLP\b', r'\bLP\b', r'\bL\.L\.C\b']),
        ('trust', [r'\bTRUST\b', r'\bREVOCABLE\b', r'\bTRUSTEE\b']),
        ('estate', [r'\bESTATE\b', r'\bEXECUTOR\b', r'\bADMINISTRATOR\b',
                    r'\bHEIR\b', r'\bPROBATE\b']),
    ]
    for otype, pats in patterns:
        for pat in pats:
            if re.search(pat, name_upper):
                return otype
    return 'individual'


def is_owner_occupant(prop_addr, prop_zip, mail_street, mail_zip):
    if not mail_street or not mail_zip:
        return False
    if re.match(r'P\.?\s*O\.?\s*BOX', mail_street.upper()):
        return False
    if re.match(r'C\.?\s*O\.', mail_street.upper()):
        return False
    if prop_zip and mail_zip != prop_zip:
        return False
    def leading(s):
        m = re.match(r'(\d+)\s+(\w+)', s.upper().strip())
        return (m.group(1), m.group(2)) if m else (None, None)
    pn, ps = leading(prop_addr)
    mn, ms = leading(mail_street)
    return pn and mn and pn == mn and ps == ms


def build_mailing_string(street, unit, line2, city, state, zipcode):
    parts = [street, unit, line2]
    street_parts = [p.strip() for p in parts if p.strip()]
    if not street_parts and city:
        return f"{city}, {state} {zipcode}".strip(", ")
    street_str = " ".join(street_parts)
    return f"{street_str}, {city}, {state} {zipcode}".strip(", ")


def gislink_to_parcel_id(gislink, parcel):
    """Convert GISLINK + PARCEL columns to a unique parcel ID.
    
    GISLINK is 15 chars, e.g. '033033L A 03000' or '033001    00200'
    PARCEL is the sub-unit, e.g. '030', '002', '003   C420'
    
    DB format: '033L A 030' or '001 002' or '001 002 C001'
    """
    if not gislink or not parcel:
        return None
    inner = gislink[3:].rstrip('0')
    inner = ' '.join(inner.split())
    parcel_clean = ' '.join(parcel.strip().split())
    
    inner_parts = inner.split()
    parcel_parts = parcel_clean.split()
    
    if parcel_parts and inner_parts and parcel_parts[0] == inner_parts[-1]:
        if len(parcel_parts) == 1:
            return inner
        else:
            return f"{inner} {' '.join(parcel_parts[1:])}"
    else:
        return f"{inner} {parcel_clean}"


def load_csv_records(csv_path, db_addresses, db_parcels):
    """Parse CSV and return records for properties NOT in our DB."""
    records = []
    
    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        col_map = {col: i for i, col in enumerate(header)}
        
        for row in reader:
            try:
                addr = col_map.get('ST_ADDRESS', -1)
                owner = col_map.get('OWNER_NAME_1', -1)
                mail = col_map.get('MAIL_ST_NAME', -1)
                unit = col_map.get('MAIL_UNIT', -1)
                line2 = col_map.get('MAIL_LINE_2', -1)
                city = col_map.get('MAIL_CITY', -1)
                state = col_map.get('MAIL_STATE', -1)
                zipcode = col_map.get('MAIL_ZIP', -1)
                assessed = col_map.get('ASSESSED_VALUE', -1)
                gislink = col_map.get('GISLINK', -1)
                parcel_idx = col_map.get('PARCEL', -1)
                sale1_date = col_map.get('SALE_1_DATE', -1)
                sale1_price = col_map.get('SALE_1_CONSIDERATION', -1)
                
                prop_addr = row[addr].strip() if addr < len(row) else ''
                prop_addr_upper = prop_addr.upper()
                
                if not prop_addr or prop_addr_upper in db_addresses:
                    continue
                
                mail_state = row[state].strip().upper() if state < len(row) else ''
                mail_zip = row[zipcode].strip() if zipcode < len(row) else ''
                mail_city = row[city].strip() if city < len(row) else ''
                
                if mail_state != 'TN':
                    continue
                if mail_zip not in LOCAL_ZIPS:
                    continue
                
                owner_name = row[owner].strip() if owner < len(row) else ''
                if not owner_name:
                    continue
                
                mail_street = row[mail].strip() if mail < len(row) else ''
                mail_unit = row[unit].strip() if unit < len(row) else ''
                mail_line2 = row[line2].strip() if line2 < len(row) else ''
                
                prop_city = ZIP_TO_CITY.get(mail_zip, mail_city)
                if not prop_city:
                    prop_city = 'Chattanooga'
                
                assessed_val = parse_number(row[assessed].strip() if assessed < len(row) else '')
                sale1_date = parse_date(row[sale1_date].strip() if sale1_date < len(row) else '')
                sale1_price = parse_number(row[sale1_price].strip() if sale1_price < len(row) else '')
                
                gislink_val = row[gislink].strip() if gislink < len(row) else ''
                parcel_val = row[parcel_idx].strip() if parcel_idx < len(row) else ''
                parcel_id = gislink_to_parcel_id(gislink_val, parcel_val)
                
                if not parcel_id or parcel_id in db_parcels:
                    continue
                
                mailing_str = build_mailing_string(
                    mail_street, mail_unit, mail_line2,
                    mail_city, mail_state, mail_zip
                )
                occupant = is_owner_occupant(prop_addr, mail_zip, mail_street, mail_zip)
                owner_type = classify_owner_type(owner_name)
                
                records.append({
                    'parcel_id': parcel_id,
                    'address': prop_addr,
                    'city': prop_city,
                    'state': 'TN',
                    'postal_code': mail_zip or '37401',
                    'owner_of_record': owner_name,
                    'owner_mailing_address': mailing_str,
                    'owner_occupant': occupant,
                    'assessed_value': assessed_val,
                    'owner_type': owner_type,
                    'sale1_price': sale1_price,
                    'sale1_date': sale1_date,
                })
                
            except Exception as e:
                log.debug("Row parse error: %s", e)
    
    return records


def run(csv_path, dry_run, limit=0):
    """Load new properties from CSV into DB."""
    
    log.info("Loading CSV: %s", csv_path)
    
    conn = psycopg2.connect(db_dsn())
    
    try:
        # Get existing DB addresses AND parcel IDs
        with conn.cursor() as cur:
            cur.execute("SELECT upper(trim(address)) FROM properties WHERE address != ''")
            db_addresses = set(row[0] for row in cur.fetchall())
        with conn.cursor() as cur:
            cur.execute("SELECT parcel_id FROM properties")
            db_parcels = set(row[0] for row in cur.fetchall())
        
        log.info("Existing DB addresses: %d", len(db_addresses))
        log.info("Existing DB parcels: %d", len(db_parcels))
        
        # Parse CSV
        records = load_csv_records(csv_path, db_addresses, db_parcels)
        log.info("New local properties found: %d", len(records))
        
        if limit:
            records = records[:limit]
            log.info("Limiting to %d records", limit)
        
        if dry_run:
            log.info("DRY RUN — skipping DB writes")
            if records:
                sample = records[0]
                log.info("Sample: %s | parcel=%s | owner=%s | type=%s",
                    sample['address'], sample['parcel_id'],
                    sample['owner_of_record'], sample['owner_type'])
            
            city_counts = {}
            for r in records:
                city = r['city']
                city_counts[city] = city_counts.get(city, 0) + 1
            log.info("\nBy city:")
            for city, count in sorted(city_counts.items(), key=lambda x: -x[1]):
                log.info("  %s: %d", city, count)
            
            return {'new_properties': len(records), 'dry_run': True}
        
        # Insert properties
        log.info("Inserting %d new properties...", len(records))
        
        INSERT_SQL = """
            INSERT INTO properties (
                county_id, parcel_id, address, city, state, postal_code,
                owner_of_record, owner_mailing_address, owner_occupant,
                assessed_value, last_sale_price, last_sale_date,
                deal_score, distress_score, created_at, updated_at
            ) VALUES (
                'hamilton', %(parcel_id)s, %(address)s, %(city)s, %(state)s, %(postal_code)s,
                %(owner_of_record)s, %(owner_mailing_address)s, %(owner_occupant)s,
                %(assessed_value)s, %(last_sale_price)s, %(last_sale_date)s,
                0, 0, NOW(), NOW()
            )
            ON CONFLICT (county_id, parcel_id) DO NOTHING
        """
        
        inserted = 0
        batch = []
        for rec in records:
            batch.append({
                'parcel_id': rec['parcel_id'],
                'address': rec['address'],
                'city': rec['city'],
                'state': rec['state'],
                'postal_code': rec['postal_code'],
                'owner_of_record': rec['owner_of_record'],
                'owner_mailing_address': rec['owner_mailing_address'],
                'owner_occupant': rec['owner_occupant'],
                'assessed_value': rec['assessed_value'],
                'last_sale_price': rec.get('sale1_price'),
                'last_sale_date': rec.get('sale1_date'),
            })
            
            if len(batch) >= 1000:
                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, batch)
                conn.commit()
                inserted += cur.rowcount
                log.info("  Inserted %d...", inserted)
                batch = []
        
        if batch:
            with conn.cursor() as cur:
                cur.executemany(INSERT_SQL, batch)
            conn.commit()
            inserted += cur.rowcount
        
        log.info("Total inserted: %d", inserted)
        return {'new_properties': inserted}
    
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Load new properties from bulk CSV")
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        stream=sys.stdout)
    
    csv_path = '/tmp/AssessorExport.csv'
    if args.csv:
        csv_path = args.csv
    
    if not os.path.exists(csv_path):
        log.error("CSV not found: %s", csv_path)
        sys.exit(1)
    
    result = run(csv_path, args.dry_run, args.limit)
    
    print(f"\n{'='*50}")
    print(f"Results:")
    print(f"  New properties: {result.get('new_properties', 0):,}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
