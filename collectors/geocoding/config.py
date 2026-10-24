"""Shared configuration for the geocoding package.

Single source of truth for the DB DSN, geocoder endpoints, rate limits, and the
unit-suffix regex that the old scripts each duplicated (with inconsistent values).
"""
from __future__ import annotations

import os
import re

from dotenv import load_dotenv
load_dotenv()

# Database — env first, dev fallback (matches the rest of the codebase).
DB_DSN = os.getenv("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")

# US Census batch geocoder (free, no key, up to 10k rows/request).
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_BENCHMARK = "Public_AR_Current"
CENSUS_BATCH_SIZE = 200  # rows per POST

# Nominatim (OpenStreetMap). ToS: <= 1 req/s, identify with a UA string.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = os.getenv(
    "PROPTOIT_GEOCODER_UA",
    "PropToIt/1.0 (property research; https://proptoit.com)",
)
REQUEST_INTERVAL = 1.1  # seconds between Nominatim requests
NOMINATIM_MAX_RETRIES = 3  # on 429/503/509 with linear backoff

# Strip condo/unit suffixes that confuse geocoders: "100 MARKET ST C001" -> "100 MARKET ST"
UNIT_RE = re.compile(r"\s+[CL]\d{3,}$", re.IGNORECASE)
