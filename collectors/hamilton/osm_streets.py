#!/usr/bin/env python3
"""
Hamilton County Street List Extractor — OpenStreetMap/Overpass API
==================================================================

Queries Overpass API for all street names in Hamilton County, TN.
Outputs deduplicated, normalized street list to JSON.

Usage:
  python3 osm_streets.py
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import logging

log = logging.getLogger(__name__)

OUTPUT_PATH = os.path.expanduser("~/proptoit/data/streets.json")

# Hamilton County, TN bounding box
HAMILTON_BOUNDS = {
    "north": 35.55,
    "south": 35.00,
    "west": -85.60,
    "east": -84.95,
}

OVERPASS_URL = "http://overpass-api.de/api/interpreter"


def query_overpass() -> list[dict]:
    """Query Overpass API for all named highways within Hamilton County bounds."""
    b = HAMILTON_BOUNDS
    query = f"""
    [out:json][timeout:90];
    way["highway"]["name"]["area"!="yes"]({b["south"]},{b["west"]},{b["north"]},{b["east"]});
    out tags;
    """
    
    log.info("Querying Overpass API...")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=query.encode(),
        headers={"Accept": "application/json", "User-Agent": "PropToIt/1.0"},
    )
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.error("Overpass query failed: %s", e)
        sys.exit(1)
    
    return data.get("elements", [])


def extract_names(elements: list[dict]) -> list[str]:
    """Extract unique, normalized street names from Overpass elements."""
    names = set()
    
    for elem in elements:
        tags = elem.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue
        
        # Normalize: uppercase, collapse whitespace
        cleaned = re.sub(r"[^\w\s]", " ", name)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
        
        if len(cleaned) >= 2:
            names.add(cleaned)
    
    # Filter noise
    skip = {"US", "ROUTE", "HIGHWAY", "EXPRESSWAY", "INTERSTATE", "COUNTY", "CITY"}
    filtered = [n for n in names if n not in skip]
    
    return sorted(filtered)


def save(streets: list[str]) -> None:
    """Save street list to JSON."""
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    
    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "source": "OpenStreetMap/Overpass API",
            "county": "Hamilton County, TN",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": len(streets),
            "streets": streets,
        }, f, indent=2)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )
    
    elements = query_overpass()
    log.info("Overpass returned %d elements", len(elements))
    
    streets = extract_names(elements)
    log.info("Extracted %d unique street names", len(streets))
    
    save(streets)
    log.info("Saved to %s", OUTPUT_PATH)
    
    print(f"\nFirst 20 streets:")
    for s in streets[:20]:
        print(f"  {s}")
    print(f"\nTotal: {len(streets)} streets")


if __name__ == "__main__":
    main()
