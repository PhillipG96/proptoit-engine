#!/usr/bin/env python3
"""
Meilisearch Indexer — Imports all PropToIt properties into Meilisearch.
Runs daily via cron at 2AM. Sends Telegram notification on completion.
"""

import csv
import requests
import logging
import subprocess
import tempfile
import os
import json

MEILISEARCH_URL = os.environ.get("MEILISEARCH_URL", "http://localhost:7707")
MEILISEARCH_KEY = os.environ.get("MEILISEARCH_ADMIN_KEY")
if not MEILISEARCH_KEY:
    raise RuntimeError("MEILISEARCH_ADMIN_KEY environment variable is required")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

log = logging.getLogger(__name__)

def export_properties():
    """Export all properties from Postgres as CSV."""
    query = """
        SELECT id, parcel_id, address, city, state, postal_code,
            lot_size_sqft, building_sqft, year_built, bedrooms, bathrooms,
            last_sale_price, assessed_value, tax_amount, owner_of_record,
            lat, lng, is_vacant, is_tax_delinquent, distress_score,
            pre_foreclosure_flag, code_violation_flag, probate_flag, fsbo_flag,
            county_id, property_category
        FROM properties ORDER BY id
    """
    result = subprocess.run(
        ['psql', '-h', 'localhost', '-U', 'proptoit', '-d', 'proptoit',
         '--csv', '-c', query],
        capture_output=True, text=True,
         env={**os.environ, 'PGPASSWORD': os.environ.get('PGPASSWORD', '')}
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr}")
    return result.stdout

def main():
    log.info("MEILISEARCH INDEXER")
    total = 0
    error = None
    
    try:
        # Export properties
        csv_text = export_properties()
        reader = csv.DictReader(csv_text.splitlines())
        docs = []
        for row in reader:
            try:
                county = row.get('county_id', 'hamilton')
                state = row.get('state', 'TN')
                docs.append({
                    'id': int(row['id']), 'parcel_id': row['parcel_id'],
                    'address': f"{row['address']}, {row['city']}, {state}",
                    'city': row['city'], 'postal_code': row['postal_code'],
                    'county_id': county,
                    'property_category': row.get('property_category', 'unknown'),
                    'bedrooms': int(row['bedrooms']) if row['bedrooms'] else None,
                    'bathrooms': int(row['bathrooms']) if row['bathrooms'] else None,
                    'lot_size_sqft': float(row['lot_size_sqft']) if row['lot_size_sqft'] else None,
                    'building_sqft': float(row['building_sqft']) if row['building_sqft'] else None,
                    'year_built': int(row['year_built']) if row['year_built'] else None,
                    'last_sale_price': float(row['last_sale_price']) if row['last_sale_price'] else None,
                    'assessed_value': float(row['assessed_value']) if row['assessed_value'] else None,
                    'tax_amount': float(row['tax_amount']) if row['tax_amount'] else None,
                    'owner_of_record': row['owner_of_record'],
                    'lat': float(row['lat']) if row['lat'] else None,
                    'lng': float(row['lng']) if row['lng'] else None,
                    'is_vacant': row['is_vacant'].lower() == 't',
                    'is_tax_delinquent': row['is_tax_delinquent'].lower() == 't',
                    'distress_score': float(row['distress_score']) if row['distress_score'] else 0,
                    'pre_foreclosure_flag': row.get('pre_foreclosure_flag','').lower() == 't',
                    'code_violation_flag': row.get('code_violation_flag','').lower() == 't',
                    'probate_flag': row.get('probate_flag','').lower() == 't',
                    'fsbo_flag': row.get('fsbo_flag','').lower() == 't',
                })
            except (ValueError, KeyError):
                pass
        
        log.info(f"Built {len(docs)} docs")
        
        # Create index
        headers = {'Authorization': f'Bearer {MEILISEARCH_KEY}'}
        requests.post(f'{MEILISEARCH_URL}/indexes', json={'uid': 'properties', 'primaryKey': 'id'}, headers=headers)
        
        # Import in batches of 50
        for i in range(0, len(docs), 50):
            r = requests.put(f'{MEILISEARCH_URL}/indexes/properties/documents?primaryKey=id', json=docs[i:i+50], headers=headers)
            if r.status_code == 202:
                total += len(docs[i:i+50])
            else:
                error = f"Batch {i//50+1} failed: {r.status_code}"
                log.error(error)
        
        log.info(f"Total: {total}")
        
    except Exception as e:
        error = str(e)
        log.error(f"Error: {error}")
    
    # Telegram notification
    message = f"{'✅ Meilisearch indexed {total} properties' if not error else f'❌ Indexer error: {error}'}"
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': message},
                timeout=10
            )
        except Exception as e:
            log.warning(f"Telegram notify failed: {e}")
    
    return total

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    imported = main()
    print(f"\n✅ Imported {imported} properties to Meilisearch")
