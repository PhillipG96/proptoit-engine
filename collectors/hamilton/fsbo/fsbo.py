#!/usr/bin/env python3
"""
FSBO (For Sale By Owner) Collector — Chattanooga, TN

Aggregates FSBO listings from multiple sources:
- Craigslist (Chattanooga section)
- Zillow FSBO section
- ForSaleByOwner.com

Outputs: fsbo_flag distress signal

Usage:
  python3 fsbo.py [--dry-run] [--force-refresh]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urljoin

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))
from base import normalize_record, save_cache, load_cache, get_scraped_at

MAX_RETRIES = 3
RETRY_DELAY = 2

# Sources
SOURCES = [
    {
        "name": "Craigslist Chattanooga",
        "base_url": "https://chattanooga.craigslist.org",
        "search_paths": ["/躲/apa", "/躲/roo", "/躲/arl"],
        "city": "Chattanooga",
    },
    {
        "name": "Zillow FSBO",
        "base_url": "https://www.zillow.com",
        "search_path": "/fsbo/chattanooga-tn/",
        "city": "Chattanooga",
    },
    {
        "name": "ForSaleByOwner",
        "base_url": "https://www.forsalebyowner.com", 
        "search_path": "/tn/chattanooga",
        "city": "Chattanooga",
    },
]

CACHE_PATH = "/tmp/proptoit/fsbo.cache"
OUTPUT_PATH = "/tmp/proptoit/fsbo.json"


def fetch_page(url, retries=MAX_RETRIES):
    """Fetch a webpage with retries."""
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Proptoit-Collector/1.0)"
            })
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries - 1:
                return None
            import time
            time.sleep(RETRY_DELAY)
    return None


def crawl_craigslist():
    """Crawl Craigslist Chattanooga for housing/real estate FSBO listings."""
    results = []
    base = "https://chattanooga.craigslist.org"
    
    # Search sections for housing and rooms
    paths = ["/deb/hsh/", "/deb/roo/", "/deb/arl/", "/deb/rep/"]
    
    for path in paths:
        url = base + path
        try:
            html = fetch_page(url)
            if not html:
                continue
            
            # Parse Craigslist listing posts
            # Craigslist uses <p class="result-info"> for meta
            post_pattern = re.compile(
                r'<a class="result-title" href="([^"]+)"[^>]*>([^<]+)</a>',
                re.DOTALL
            )
            
            price_pattern = re.compile(r'\$([\d,]+)', re.I)
            date_pattern = re.compile(r'(\w{3}\s+\d{1,2},\s+\d{4})')
            
            for match in post_pattern.finditer(html):
                href = match.group(1)
                title = match.group(2).strip()
                
                # Check if title contains buying/renting keywords
                if "fsbo" in title.lower() or "by owner" in title.lower():
                    price_match = price_pattern.search(match.group(0))
                    price = price_match.group(1) if price_match else "N/A"
                    
                    date_match = date_pattern.search(match.group(0))
                    date = date_match.group(1) if date_match else datetime.now().strftime("%b %d, %Y")
                    
                    record = normalize_record(
                        parcel_id=f"cl-{hash(href)}",
                        address=f"Chattanooga, TN (Craigslist - {title[:50]})",
                        lat=35.0456,
                        lng=-85.3097,
                        source="fsbo",
                        raw_data={
                            "platform": "craigslist",
                            "craigslist_url": href,
                            "title": title,
                            "price": price,
                            "listed_date": date,
                        },
                        distress_signals=["fsbo_flag"]
                    )
                    results.append(record)
        except Exception as e:
            print(f"  Craigslist error in {path}: {e}")
    
    return results


def crawl_zillow_fsbo():
    """Crawl Zillow FSBO section for Chattanooga."""
    results = []
    url = "https://www.zillow.com/fsbo/chattanooga-tn/"
    
    try:
        html = fetch_page(url)
        if not html:
            return results
        
        # Parse Zillow listing cards
        card_pattern = re.compile(
            r'<div[^>]*class="[^"]*card[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL
        )
        
        for match in card_pattern.finditer(html):
            card = match.group(1)
            if "Zillow FSBO" in card or "For Sale By Owner" in card:
                # Extract address and price
                addr_pattern = re.compile(r'([0-9]+\s+[\w\s]+\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard),\s*Chattanooga)', re.I)
                price_pattern = re.compile(r'\$([\d,]+)', re.I)
                
                addr_m = addr_pattern.search(card)
                addr = addr_m.group(1) if addr_m else "Chattanooga, TN"
                
                price_m = price_pattern.search(card)
                price = price_m.group(1) if price_m else "N/A"
                
                record = normalize_record(
                    parcel_id=f"zillow-{hash(card[:100])}",
                    address=addr,
                    lat=35.0456,
                    lng=-85.3097,
                    source="fsbo",
                    raw_data={
                        "platform": "zillow",
                        "zillow_url": url,
                        "price": price,
                    },
                    distress_signals=["fsbo_flag"]
                )
                results.append(record)
    except Exception as e:
        print(f"  Zillow error: {e}")
    
    return results


def crawlforsalebyowner():
    """Crawl ForSaleByOwner.com Chattanooga section."""
    results = []
    url = "https://www.forsalebyowner.com/tn/chattanooga"
    
    try:
        html = fetch_page(url)
        if not html:
            return results
        
        # Parse listing cards
        card_pattern = re.compile(
            r'<div[^>]*class="[^"]*listing[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL
        )
        
        price_pattern = re.compile(r'\$([\d,]+)', re.I)
        
        for match in card_pattern.finditer(html):
            card = match.group(1)
            if "for sale" in card.lower():
                
                price_m = price_pattern.search(card)
                price = price_m.group(1) if price_m else "N/A"
                
                record = normalize_record(
                    parcel_id=f"fsbo-dot-com-{hash(card[:100])}",
                    address="Chattanooga, TN",
                    lat=35.0456,
                    lng=-85.3097,
                    source="fsbo",
                    raw_data={
                        "platform": "forsalebyowner.com",
                        "url": url,
                        "price": price,
                    },
                    distress_signals=["fsbo_flag"]
                )
                results.append(record)
    except Exception as e:
        print(f"  ForSaleByOwner error: {e}")
    
    return results


def run(dry_run=False, force_refresh=False):
    """Run the FSBO collector."""
    print("=" * 60)
    print("FSBO (FOR SALE BY OWNER) COLLECTOR — Chattanooga, TN")
    print("=" * 60)
    
    # Load cache
    if not force_refresh:
        cache = load_cache(CACHE_PATH, default=[])
        print(f"Loaded {len(cache)} cached records")
        seen_ids = {r.get("parcel_id") for r in cache}
    else:
        seen_ids = set()
        cache = []
    
    # Fetch from all sources
    new_records = []
    print("\n>>> Crawling Craigslist...")
    new_records.extend(crawl_craigslist())
    
    print(">>> Crawling Zillow FSBO...")
    new_records.extend(crawl_zillow_fsbo())
    
    print(">>> Crawling ForSaleByOwner...")
    new_records.extend(crawlforsalebyowner())
    
    print(f"\nFetched {len(new_records)} new records")
    
    # Filter duplicates
    fresh_records = [r for r in new_records if r["parcel_id"] not in seen_ids]
    print(f"New/unseen records: {len(fresh_records)}")
    
    # Combine with cache
    all_records = cache + fresh_records
    
    if dry_run:
        print("\n--- DRY RUN — Would write to:", OUTPUT_PATH)
        for r in fresh_records[:5]:
            print(f"  {r['parcel_id']}: {r['address']} [{', '.join(r['distress_signals'])}]")
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
    parser = argparse.ArgumentParser(description="FSBO Collector")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(dry_run=args.dry_run, force_refresh=args.force_refresh)
