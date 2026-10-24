#!/usr/bin/env python3
"""
Tax History Parser — Hamilton County Property Tax Records

Parses Hamilton County assessor tax records to build a historical tax timeline.
Tracks annual assessments, tax amounts, payment status, and identifies delinquent properties.

Status: PRO-4 (W3-4) — Tax history ingestion
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("proptoit.tax_history")

# ─── Configuration ─────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
TAX_SOURCE_URL = os.environ.get(
    "TAX_SOURCE_URL", "https://www.hamiltoncountytn.gov/records/assessor"
)
DB_DSN = os.environ.get(
    "DB_DSN",
    os.getenv("DATABASE_URL"),
)

# Tax year extraction patterns
TAX_YEAR_PATTERN = re.compile(r"(?:FY|Tax Year|Year)\s*(\d{4})")

# Amount extraction patterns
AMOUNT_PATTERN = re.compile(r"\$(\d[\d,\.]*)(?:\s*USD)?")

# Delinquency patterns
DELINQUENT_PATTERNS = [
    r"(delinqu|due|overdue|arrears|unpaid|past\s*due)",
    r"(lien|tax\s*lien|foreclo[rs]e|[fp]d)",
    r"(holed|+\d+[fh]|f\+h)",  # Hamilton county specific format
]

# ─── Tax Value Normalization ───────────────────────────────────────────────

def normalize_tax_amount(value: str) -> Optional[float]:
    """
    Normalize tax amount from various formats.
    Examples: "$500.00", "$500", "500.00", "500", "5,000.00"
    """
    if not value or value in ("N/A", "", "NULL", "null", "0.00"):
        return None

    # Remove dollar signs and commas
    cleaned = value.replace("$", "").replace(",", "").strip()

    try:
        return float(cleaned)
    except ValueError:
        logger.debug(f"Could not parse amount: {value}")
        return None


def normalize_assessment(value: str) -> Optional[float]:
    """Normalize assessed value with greater tolerance."""
    if not value or value in ("N/A", "", "NULL", "null"):
        return None

    # Handle "approximate" notation
    cleaned = value.replace("$", "").replace(",", "").replace("~", "").strip()

    try:
        return float(cleaned)
    except ValueError:
        logger.debug(f"Could not parse assessment: {value}")
        return None


# ─── Tax Record Parser ─────────────────────────────────────────────────────

class TaxHistoryParser:
    """Parses and ingests Hamilton County tax history records."""

    def __init__(self, db_dsn: str = DB_DSN) -> None:
        self.dsn = db_dsn
        self.delinquency_patterns = [re.compile(p, re.IGNORECASE) for p in DELINQUENT_PATTERNS]

    def extract_tax_year(self, raw_text: str) -> Optional[int]:
        """Extract tax year from various text formats."""
        match = TAX_YEAR_PATTERN.search(raw_text)
        if match:
            return int(match.group(1))

        # Try to extract from a 4-digit number near "year" or "FY"
        year_match = re.search(r"(?:20|19)\d{2}", raw_text)
        if year_match:
            year = int(year_match.group())
            if 1990 <= year <= 2100:
                return year

        return None

    def determine_payment_status(self, text: str) -> str:
        """
        Determine payment status from various indicators.
        Returns: 'paid', 'unpaid', 'delinquent', 'partial', 'pending'
        """
        text_lower = text.lower()

        for pattern in self.delinquency_patterns:
            if pattern.search(text):
                if "lien" in text_lower or "foreclos" in text_lower:
                    return "delinquent"
                return "unpaid"

        # NB: "$0.00"/"0.00" removed — delinquent notices carry $0.00 balance
        # subfields which this loop used to hit before the amount logic ran.
        paid_indicators = ["paid", "cleared", "settled", "discharged", "no li"]
        for indicator in paid_indicators:
            if indicator in text_lower:
                return "paid"

        # NB: was `or "p" in text_lower` — matched virtually every row
        # ("parcel", "unpaid", "Penalty", ...) and swallowed the branches
        # below. Only an explicit 'partial' means partial.
        if "partial" in text_lower:
            return "partial"

        # No explicit paid/delinquent marker -> be honest about it.
        # (No 'pending' branch: it was unreachable behind the
        # partial/'p'-suffix trap that used to swallow every row.)
        return "unknown"

    def classify_record_type(self, raw_text: str) -> str:
        """
        Classify what type of tax record this is.
        Returns: 'annual_tax', 'assessment', 'penalty', 'interest', 'lien'
        """
        text_lower = raw_text.lower()

        if "pen" in text_lower and ("alty" in text_lower or "%" in text_lower):
            return "penalty"
        if "interest" in text_lower:
            return "interest"
        if "lien" in text_lower:
            return "lien"
        if "assessment" in text_lower or "assessed" in text_lower:
            return "assessment"

        return "annual_tax"

    def parse_single_record(self, raw_line: Any) -> dict:
        """
        Parse a single tax record line into structured data.
        """
        if isinstance(raw_line, str):
            text = raw_line
        elif isinstance(raw_line, dict):
            # _fetch_raw_tax_data returns csv.DictReader rows (dicts).
            # str(dict) produced a Python repr ("{'Parcel ID': ...}") that
            # the regexes below were parsing — join values on tabs instead.
            text = "\t".join("" if v is None else str(v) for v in raw_line.values())
        else:
            text = str(raw_line)

        # Extract parcel ID
        parcel_match = re.search(r"(PCL|PARCEL|PARCEL ID|ID)\s*[:\s\-]*(\S+)", text, re.IGNORECASE)
        parcel_id = parcel_match.group(2) if parcel_match else None

        # If no parcel_id, extract from beginning of line
        if not parcel_id:
            parts = text.split()
            if parts and parts[0].replace("-", "").isdigit():
                parcel_id = parts[0]

        year = self.extract_tax_year(text)
        amount_match = re.search(r"\$(\d[\d,\.]*)", text)
        amount = normalize_tax_amount(amount_match.group(1) if amount_match else "")

        status = self.determine_payment_status(text)
        record_type = self.classify_record_type(text)

        return {
            "parcel_id": parcel_id,
            "tax_year": year,
            "amount": amount,
            "status": status,
            "record_type": record_type,
            "raw_text": text[:200],  # Truncate for storage
            "parsed_at": datetime.now().isoformat(),
        }

    def parse_tax_data(self, raw_data: list) -> list[dict]:
        """Parse a batch of raw tax records into structured format."""
        parsed = []

        for raw in raw_data:
            record = self.parse_single_record(raw)
            if record["parcel_id"] and record["tax_year"]:
                parsed.append(record)

        logger.info(f"Parsed {len(parsed)} records from {len(raw_data)} raw entries")
        return parsed

    def get_tax_delinquency_flags(self, tax_records: list[dict]) -> dict:
        """
        Generate tax delinquency flags for a property based on all tax records.
        Returns flags dict for the distress_signals table.
        """
        flags = {
            "is_tax_delinquent": False,
            "tax_history_years": 0,
            "missing_years": 0,
            "unpaid_years": 0,
            "total_unpaid_amount": 0.0,
            "highest_lien_year": None,
            "latest_year": None,
            "oldest_year": None,
        }

        if not tax_records:
            return flags

        years = []
        unpaid_amount = 0.0
        highest_lien_year = None
        latest_year = None
        oldest_year = None

        for record in tax_records:
            year = record["tax_year"]
            if year:
                years.append(year)

                if record["status"] in ("delinquent", "unpaid"):
                    flags["unpaid_years"] += 1
                    unpaid_amount += record["amount"] or 0

                if record["record_type"] == "lien" and not highest_lien_year:
                    highest_lien_year = year

                if not latest_year or year > latest_year:
                    latest_year = year
                if not oldest_year or year < oldest_year:
                    oldest_year = year

        flags["tax_history_years"] = len(years)
        flags["latest_year"] = latest_year
        flags["oldest_year"] = oldest_year
        flags["total_unpaid_amount"] = round(unpaid_amount, 2)

        # Flag as delinquent if any unpaid years
        flags["is_tax_delinquent"] = flags["unpaid_years"] > 0

        # Mark missing years (gaps in history)
        if years:
            full_range = set(range(min(years), max(years) + 1))
            flags["missing_years"] = len(full_range - set(years))

        return flags

    def upsert_tax_record(self, parcel_id: str, tax_data: dict) -> int:
        """
        Upsert a single tax history record.
        Returns PQ counter (1=insert, 2=update).
        """
        logger.info(f"Upserting tax record for {parcel_id}, year {tax_data['tax_year']}")
        return 1  # Simplified — actual DB logic can be added

    def ingest_tax_history(self, force: bool = False) -> dict:
        """
        Main tax history ingestion pipeline.
        Returns summary statistics.
        """
        start_time = datetime.now()
        stats = {
            "records_processed": 0,
            "records_inserted": 0,
            "records_updated": 0,
            "tax_records_by_year": {},
            "delinquent_properties": 0,
            "source": "hamilton_co_assessor",
        }

        # 1. Fetch raw tax data
        tax_records_raw = self._fetch_raw_tax_data()
        logger.info(f"Raw tax records fetched: {len(tax_records_raw)}")

        # 2. Parse tax records
        parsed = self.parse_tax_data(tax_records_raw)

        # 3. Group by parcel and compute flags
        parcel_tax_groups = {}
        for record in parsed:
            parcel_id = record["parcel_id"]
            if parcel_id not in parcel_tax_groups:
                parcel_tax_groups[parcel_id] = []
            parcel_tax_groups[parcel_id].append(record)

        # 4. Upsert records and update flags
        for parcel_id, records in parcel_tax_groups.items():
            flags = self.get_tax_delinquency_flags(records)

            # Track year distribution
            for record in records:
                year = record["tax_year"]
                if year:
                    stats["tax_records_by_year"][year] = \
                        stats["tax_records_by_year"].get(year, 0) + 1

            # Track delinquent properties
            if flags.get("is_tax_delinquent"):
                stats["delinquent_properties"] += 1

            # Upsert all records for this parcel
            for record in records:
                pq_counter = self.upsert_tax_record(parcel_id, record)
                stats["records_inserted"] += pq_counter
                stats["records_processed"] += 1

        elapsed = (datetime.now() - start_time).total_seconds()
        stats["elapsed_seconds"] = round(elapsed, 2)
        stats["completed_at"] = datetime.now().isoformat()

        logger.info(f"Tax history ingestion complete: {stats}")
        return stats

    def _fetch_raw_tax_data(self) -> list:
        """Fetch raw tax data from source."""
        import os
        tax_dir = BASE_DIR.parent / "data" / "tax"
        tax_file = tax_dir / "assessor.csv"

        if tax_file.exists():
            import csv
            with open(tax_file, "r", encoding="utf-8") as f:
                return list(csv.DictReader(f))

        # Fallback: return empty list
        logger.debug(f"No tax file found at {tax_file}")
        return []


def main():
    """Run as standalone script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    parser = TaxHistoryParser()
    stats = parser.ingest_tax_history()

    logger.info("\n" + "=" * 50)
    logger.info(f"TAX HISTORY INGESTION COMPLETE")
    logger.info("=" * 50)
    logger.info(f"Records processed: {stats['records_processed']}")
    logger.info(f"Records inserted:  {stats['records_inserted']}")
    logger.info(f"DelinquentProps:   {stats['delinquent_properties']}")
    logger.info(f"Years covered:     {len(stats['tax_records_by_year'])}")
    if stats['elapsed_seconds']:
        logger.info(f"Duration:          {stats['elapsed_seconds']}s")
    logger.info(f"Completed at:      {stats['completed_at']}")


if __name__ == "__main__":
    main()
