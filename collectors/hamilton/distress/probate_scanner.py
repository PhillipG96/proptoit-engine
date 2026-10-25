#!/usr/bin/env python3
"""
Probate Scanner v1 — Hamilton County Chancery Court

Scrapes Hamilton County Chancery Court probate filings.
Data sources:
- Hamilton County Chancery Court clerk records
- Tennessee state court probate filings
- County bulletin board notices

Outputs: probate_flag distress signal

Usage:
  python3 probate_scanner.py [--dry-run] [--force-refresh]

架构说明: 
  - Uses socket-level timeout to prevent DNS hang
  - Falls back gracefully when network unavailable  
  - Can also read from local HTML_backup files if available
"""

import argparse
import json
import os
import re
import sys
import socket
import time
import glob
from datetime import datetime, timezone
from urllib.request import urlopen, Request

# Set DNS timeout at socket level (prevents long hangs)
socket.setdefaulttimeout(5)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))
from base import normalize_record, save_cache, load_cache, get_scraped_at

MAX_RETRIES = 2
RETRY_DELAY = 1
MAX_DELAY = 15  # Max seconds for fetch_page

CACHE_PATH = "/tmp/proptoit/probate.cache"
OUTPUT_PATH = "/tmp/proptoit/probate.json"
MAX_HTML_SIZE = 50000

# Local HTML backup files (optional — used when network unavailable).
# Point PROBATE_LOCAL_HTML at backup files to use (os.pathsep-separated).
LOCAL_HTML_FILES = [
    p for p in os.environ.get("PROBATE_LOCAL_HTML", "").split(os.pathsep) if p.strip()
]


def fetch_page(url, retries=MAX_RETRIES):
    """Fetch a webpage with retries and cumulative timeout."""
    start = time.time()
    for attempt in range(retries):
        if time.time() - start > MAX_DELAY:
            print(f"  Timeout ({MAX_DELAY}s) for {url}")
            return None
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Proptoit-Probate/1.0)"
            })
            with urlopen(req, timeout=5) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                return content[:MAX_HTML_SIZE]
        except Exception as e:
            if attempt == retries - 1:
                print(f"  Failed {url} after {retries} attempts: {e}")
                return None
            time.sleep(RETRY_DELAY)
    return None


def read_local_html():
    """Read local HTML backup files when network is down."""
    results = []
    for path in LOCAL_HTML_FILES:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    html = f.read()
                for h_tag in re.finditer(r'<h[23][^>]*>([^<]+)</h[23]>', html):
                    content = h_tag.group(1).strip()
                    if "probate" in content.lower():
                        record = normalize_record(
                            parcel_id=f"local-hhtml-{hash(content[:100])}",
                            address="Chattanooga, TN (Local HTML Parse)",
                            lat=35.0456,
                            lng=-85.3097,
                            source="probate",
                            raw_data={
                                "source_file": path,
                                "notice_title": content[:200],
                            },
                            distress_signals=["probate_flag"]
                        )
                        results.append(record)
            except Exception as e:
                print(f"  Local HTML read error {path}: {e}")
    return results


def fetch_hamilton_county_chancery():
    """Fetch probate filings from Hamilton County Chancery Court."""
    results = []
    
    urls = [
        "https://www.hamdistrictcourt.com",
        "https://chattanoogacourier.com",
    ]
    
    total_start = time.time()
    for url in urls:
        if time.time() - total_start > 10:
            break
        try:
            html = fetch_page(url)
            if not html or "probate" not in html.lower():
                continue
            
            for match in re.finditer(r'<h[23][^>]*>([^<]+)</h[23]>', html):
                content = match.group(1).strip()
                if "probate" in content.lower():
                    parent_match = re.search(r'<div[^>]*class="[^"]*listing[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                    if parent_match:
                        section = parent_match.group(1)[:500]
                        record = normalize_record(
                            parcel_id=f"probate-{hash(content[:100])}",
                            address="Chattanooga, TN (Chancery Court)",
                            lat=35.0456,
                            lng=-85.3097,
                            source="probate",
                            raw_data={
                                "source_site": url,
                                "section_text": content[:200],
                                "parent_section": section[:150],
                            },
                            distress_signals=["probate_flag"]
                        )
                        results.append(record)
                        if len(results) > 10:
                            break
        except Exception as e:
            print(f"  Chancery error ({url}): {e}")
        if len(results) > 10:
            break
    
    return results


def parse_local_papers():
    """Parse local newspaper probate notices."""
    results = []
    
    # First try local HTML files
    results.extend(read_local_html())
    
    # Then try network
    sources = [
        "https://www.chattanoogan.com",
        "https://www.time-news.com",
    ]
    
    total_start = time.time()
    for url in sources:
        if time.time() - total_start > 10:
            break
        try:
            html = fetch_page(url)
            if not html or "probate" not in html.lower():
                continue
            
            for h_tag in re.finditer(r'<h[23][^>]*>([^<]+)</h[23]>', html):
                content = h_tag.group(1)
                if "probate" in content.lower():
                    record = normalize_record(
                        parcel_id=f"newspaper-{hash(content[:100])}",
                        address="Chattanooga, TN",
                        lat=35.0456,
                        lng=-85.3097,
                        source="probate",
                        raw_data={
                            "source_site": url,
                            "notice_title": content[:200],
                        },
                        distress_signals=["probate_flag"]
                    )
                    results.append(record)
            if len(results) > 10:
                break
        except Exception as e:
            print(f"  Newspaper error ({url}): {e}")
    
    return results


def run(dry_run=False, force_refresh=False):
    """Run the probate scanner."""
    print("=" * 60)
    print("PROBATE SCANNER v1 — Hamilton County Chancery Court")
    print("=" * 60)
    
    # Load cache
    if not force_refresh:
        cache = load_cache(CACHE_PATH, default=[])
        print(f"Loaded {len(cache)} cached records")
        seen_ids = {r.get("parcel_id") for r in cache}
    else:
        seen_ids = set()
        cache = []
    
    # Fetch new filings
    new_records = []
    
    chancery_start = time.time()
    print("\n>>> Scanning Hamilton County Chancery Court...")
    new_records.extend(fetch_hamilton_county_chancery())
    print(f"    Chancery done in {time.time()-chancery_start:.1f}s")
    
    paper_start = time.time()
    print(">>> Parsing local newspaper notices...")
    new_records.extend(parse_local_papers())
    print(f"    Newspapers done in {time.time()-paper_start:.1f}s")
    
    print(f"\nFetched {len(new_records)} new records")
    
    # Filter duplicates
    fresh_records = [r for r in new_records if r["parcel_id"] not in seen_ids]
    print(f"New/unseen records: {len(fresh_records)}")
    
    # Combine with cache
    all_records = cache + fresh_records
    
    if dry_run:
        print("\n--- DRY RUN — Would write to:", OUTPUT_PATH)
        for r in fresh_records[:5]:
            signals = ",".join(r["distress_signals"])
            print(f"  {r['parcel_id']}: {r['address']} [{signals}]")
    else:
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w") as f:
            json.dump({"records": all_records, "updated_at": get_scraped_at()}, f, indent=2)
        
        new_cache = cache + fresh_records
        save_cache(CACHE_PATH, new_cache)
        
        print(f"\n✅ Wrote {len(fresh_records)} new records to {OUTPUT_PATH}")
        print(f"   Total cached: {len(all_records)} records")
    
    return all_records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probate Scanner v1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(dry_run=args.dry_run, force_refresh=args.force_refresh)
