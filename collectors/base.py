#!/usr/bin/env python3
"""
Collector Interface — unified interface for all data collectors.

Each collector must implement:
- run() -> list[dict]  — scrape and return normalized records
- get_distress_tags(record: dict) -> list[str]  — return distress signals

All collectors output records matching the PropToIt schema:
  {
    "parcel_id": str,
    "address": str,
    "lat": float,
    "lng": float,
    "source": str,         # e.g. "foreclosure_auction", "fsbo", "probate"
    "scraped_at": str,     # ISO timestamp
    "raw_data": dict,      # original scraped data (preserved for debugging)
    "distress_signals": [],
    ...
  }
"""

import json
import os
from datetime import datetime, timezone


def get_scraped_at():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_record(parcel_id, address, lat, lng, source, raw_data=None, distress_signals=None):
    """Create a normalized collector record."""
    return {
        "parcel_id": parcel_id,
        "address": address,
        "lat": lat,
        "lng": lng,
        "source": source,
        "scraped_at": get_scraped_at(),
        "raw_data": raw_data or {},
        "distress_signals": distress_signals or []
    }


def load_cache(cache_path, default=None):
    """Load a JSON cache file, handling missing files."""
    if default is None:
        default = []
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default


def save_cache(cache_path, data):
    """Save a JSON cache file."""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2)
