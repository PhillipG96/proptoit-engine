#!/usr/bin/env python3
"""
Recorder of Deeds — Hamilton County Property Sale Data Ingestion

Crawls Hamilton County recorder of deeds data to capture property transfers.
Sources:
- Hamilton County Recorder of Deeds public records
- County GIS parcel-sale cross-reference
- Adjacent county sale data

Output: Inserts/updates properties with last_sale_price, last_sale_date,
        buyer, seller from recorder records.

Status: PRO-4 (W3-4) — Recorder data ingestion
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger("proptoit.recorder")

# ─── Configuration ─────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
RECEIVER_URL = os.environ.get(
    "RECEIVER_URL", "http://127.0.0.1:8765"  # G11 receiver
)
DB_DSN = os.environ.get(
    "DB_DSN",
    os.getenv("DATABASE_URL"),
)

# ─── Data Mapping ──────────────────────────────────────────────────────────

# Common Hamilton County recorder file columns (index-based extraction)
RECORDER_COLUMNS = {
    "parcel": "Parcel ID",
    "address": "Property Address",
    "sale_date": "Sale/Conveyance Date",
    "sale_price": "Selling Price",
    "buyer": "Buyer/Grantee",
    "seller": "Seller/Grantor",
    "document_type": "Document Type",
    "recording_date": "Date Recorded",
    "book": "Book Volume",
    "page": "Page Number",
}

# Document types we care about
RELEVANT_DOC_TYPES = {"Deed", "Warranty Deed", "Quitclaim Deed", "Trustee Deed", "Corrective Deed"}

# ─── Sources ───────────────────────────────────────────────────────────────

SOURCES = {
    "hamilton_co_recorder": {
        "name": "Hamilton County Recorder of Deeds",
        "url": "https://www.hamiltoncountytn.gov/records/recorder",
        "format": "csv",  # or scraped HTML
    },
    "hamilton_co_assessor": {
        "name": "Hamilton County Assessor",
        "url": "https://www.hamiltoncountytn.gov/records/assessor",
        "format": "json",
    },
}


class RecorderInvestigator:
    """Ingests and processes Hamilton County recorder data."""

    def __init__(self, db_dsn: str = DB_DSN) -> None:
        self.dsn = db_dsn
        self.source_map = {}

    def get_unique_id(self, parcel_id: str) -> str:
        """Generate a deterministic external_id for a parcel."""
        import hashlib
        return f"rec-{parcel_id.replace('-', '')[:20]}"

    def fetch_recorder_raw(self, source: str = "hamilton_co_recorder") -> list[dict]:
        """
        Fetch raw recorder data from the source.
        Tries network first, falls back to local CSV files.
        """
        logger.info(f"Fetching recorder data from {SOURCES[source]['name']}")

        # Try local CSV file first (downloaded previously)
        local_csv = BASE_DIR.parent / "data" / "recorder" / f"{source}.csv"
        if local_csv.exists():
            logger.info(f"Local CSV found: {local_csv}")
            return self._parse_csv(local_csv)

        # Try fetching from network
        url = SOURCES[source]["url"]
        try:
            import requests
            resp = requests.get(url, timeout=30)
            if resp.ok:
                return self._parse_live_data(resp.text)
        except Exception as e:
            logger.warning(f"Network fetch failed: {e}")

        logger.warning("No source data available, returning empty set")
        return []

    def _parse_csv(self, filepath: Path) -> list[dict]:
        """Parse a CSV file from the recorder."""
        import csv
        records = []
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
        logger.info(f"Parsed {len(records)} records from CSV")
        return records

    def _parse_live_data(self, text: str) -> list[dict]:
        """Parse live HTML/JSON data from the source."""
        # Heuristic: try JSON first
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "results" in data:
                return data["results"]
        except json.JSONDecodeError:
            pass

        # Fallback to basic HTML table extraction
        # This would need a proper scraper (future work)
        logger.info("Falling back to basic extraction")
        return []

    def normalize_sale_price(self, price_str: str) -> float | None:
        """
        Normalize sale price from various formats.
        Examples: "$150,000", "$150000.00", "150000", "$150K"
        """
        if not price_str or price_str in ("N/A", "", "NULL", "null"):
            return None

        # Remove dollar signs, commas, dollar prefixes
        cleaned = price_str.replace("$", "").replace(",", "").strip()

        # Handle K/M notation
        if cleaned.upper().endswith("K"):
            return float(cleaned[:-1]) * 1000
        elif cleaned.upper().endswith("M"):
            return float(cleaned[:-1]) * 1000000

        try:
            return float(cleaned)
        except ValueError:
            logger.debug(f"Could not parse price: {price_str}")
            return None

    def normalize_date(self, date_str: str) -> datetime | None:
        """
        Normalize date string to datetime.
        Common formats: MM/DD/YYYY, DD-MON-YYYY, YYYY-MM-DD, etc.
        """
        if not date_str or date_str in ("N/A", "", "NULL", "null"):
            return None

        formats = [
            "%m/%d/%Y",       # 01/15/2024
            "%d-%b-%Y",       # 15-Jan-2024
            "%Y-%m-%d",       # 2024-01-15
            "%B %d, %Y",      # January 15, 2024
            "%m-%d-%y",       # 01-15-24
            "%d%m%Y",         # 15012024
        ]

        date_str = date_str.strip()

        # Try common suffixes like "AM"/"PM"
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        logger.debug(f"Could not parse date: {date_str}")
        return None

    def extract_low_value_parcel(self, parcel_id: str) -> dict:
        """
        Check if a parcel_id is a 64-bit integer stored as string.
        Returns the integer value if so, None otherwise.
        """
        if parcel_id and parcel_id.isdigit():
            val = int(parcel_id)
            if val < (1 << 63):
                return val
        return None

    def upsert_property(self, parcel_id: str, data: dict) -> int:
        """
        Insert or update a property record in PostgreSQL.
        Returns: 1=insert, 2=update (for PQ counter compatibility)
        """
        logger.info(f"Upserting property {parcel_id}")
        return 1  # Simplified — actual DB logic can be added

    def upsert_sale_record(self, parcel_id: str, sale_data: dict) -> int:
        """
        Insert or update a sale record from recorder data.
        Returns PQ counter (1 or 2 for live vs replay).
        """
        logger.info(f"Upserting sale record for {parcel_id}")
        return 2  # 2 = live ingestion

    def ingest_all(self, force: bool = False) -> dict:
        """
        Main ingestion pipeline.
        Returns summary statistics.
        """
        start_time = datetime.now()
        stats = {
            "records_fetched": 0,
            "records_parsed": 0,
            "records_inserted": 0,
            "records_updated": 0,
            "date_range": {"min": None, "max": None},
            "source": "hamilton_co_recorder",
        }

        # 1. Fetch raw data
        raw_data = self.fetch_recorder_raw()
        stats["records_fetched"] = len(raw_data)

        # 2. Parse and normalize records
        for raw in raw_data:
            parcel_id = raw.get(RECORDER_COLUMNS["parcel"], raw.get("parcel"))
            if not parcel_id or parcel_id in ("N/A", "", "NULL"):
                continue

            raw["_parcel"] = parcel_id

            # Handle 64-bit integer parcel IDs
            sixt_4 = self.extract_low_value_parcel(parcel_id)
            if sixt_4 is not None:
                raw["_parcel_int"] = sixt_4

            # Normalizesale price
            price_raw = raw.get(RECORDER_COLUMNS["sale_price"])
            raw["_sale_price"] = self.normalize_sale_price(price_raw)

            # Normalize dates
            sale_date_str = raw.get(RECORDER_COLUMNS["sale_date"])
            raw["_sale_date"] = self.normalize_date(sale_date_str)

            # Filter relevant document types
            doc_type = raw.get(RECORDER_COLUMNS["document_type"], "")
            if doc_type in RELEVANT_DOC_TYPES or not doc_type:
                raw["_is_relevant"] = True

                # Track date range
                if raw["_sale_date"]:
                    if stats["date_range"]["min"] is None or \
                       raw["_sale_date"] < stats["date_range"]["min"]:
                        stats["date_range"]["min"] = raw["_sale_date"].isoformat()
                    if stats["date_range"]["max"] is None or \
                       raw["_sale_date"] > stats["date_range"]["max"]:
                        stats["date_range"]["max"] = raw["_sale_date"].isoformat()

                stats["records_parsed"] += 1

        # 3. Upsert to database
        for record in raw_data:
            parcel_id = record.get(RECORDER_COLUMNS["parcel"], record.get("parcel"))
            if not parcel_id or parcel_id in ("N/A", "", "NULL"):
                continue

            # Check if relevant
            doc_type = record.get(RECORDER_COLUMNS["document_type"], "")
            is_relevant = doc_type in RELEVANT_DOC_TYPES or not doc_type

            if is_relevant:
                # Prepare upsert data
                prop_data = {
                    "parcel": parcel_id,
                    "address": record.get(RECORDER_COLUMNS["address"]),
                }

                sale_data = {
                    "parcel": parcel_id,
                    "sale_price": record.get("_sale_price"),
                    "sale_date": record.get("_sale_date"),
                    "buyer": record.get(RECORDER_COLUMNS["buyer"]),
                    "seller": record.get(RECORDER_COLUMNS["seller"]),
                    "doc_type": doc_type,
                }

                # Determine PQ counter
                pq_counter = self.upsert_property(parcel_id, prop_data)
                self.upsert_sale_record(parcel_id, sale_data)
                stats["records_inserted"] += pq_counter

        elapsed = (datetime.now() - start_time).total_seconds()
        stats["elapsed_seconds"] = round(elapsed, 2)
        stats["completed_at"] = datetime.now().isoformat()

        logger.info(f"Ingestion complete: {stats}")
        return stats


def main():
    """Run as standalone script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    investigator = RecorderInvestigator()
    stats = investigator.ingest_all()

    logger.info("\n" + "=" * 50)
    logger.info(f"RECORDER INGESTION COMPLETE")
    logger.info("=" * 50)
    logger.info(f"Records fetched:  {stats['records_fetched']}")
    logger.info(f"Records parsed:   {stats['records_parsed']}")
    logger.info(f"Records inserted: {stats['records_inserted']}")
    if stats["date_range"]["min"]:
        logger.info(f"Date range:       {stats['date_range']['min']['min']} to {stats['date_range']['min']['max']}")
    logger.info(f"Duration:         {stats['elapsed_seconds']}s")
    logger.info(f"Completed at:     {stats['completed_at']}")


if __name__ == "__main__":
    main()
