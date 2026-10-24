#!/usr/bin/env python3
"""
Owner Occupant Heuristic Fix
=============================
For all properties with owner_mailing_address, determine owner_occupant
by comparing the mailing street address with the property address.

Mailing address format: [OWNER NAME] [CITY] TN [MAILING STREET] [ZIP]
"""
import os
import re
import sys
import psycopg2

from dotenv import load_dotenv
load_dotenv()

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn


def parse_mailing_address(raw: str) -> dict:
    """Parse 'NAME CITY TN STREET ZIP' into components."""
    if not raw:
        return {}
    parts = raw.upper().split()
    if len(parts) < 4:
        return {}

    # Zip is always the last part (may have +4)
    zip_match = re.match(r"(\d{5})(?:-\d{4})?", parts[-1])
    if not zip_match:
        return {}
    mail_zip = zip_match.group(1)

    # Find the state abbreviation (TN) to split name/city from street
    state_idx = None
    for i, p in enumerate(parts):
        if p in ("TN", "AL", "GA", "FL", "KY", "NC", "SC", "VA"):
            state_idx = i
            break
    if state_idx is None:
        return {}

    # Street is everything between state and zip
    street_parts = parts[state_idx + 1:-1]  # skip zip at end
    if not street_parts:
        return {}

    # Skip PO Box, Suite, etc.
    street_str = " ".join(street_parts)
    if re.match(r"P\.?\s*O\.?\s*BOX", street_str):
        return {"zip": mail_zip, "street": None}

    return {"zip": mail_zip, "street": street_str}


def leading_address(street: str):
    """Extract (number, first_word) from a street address."""
    if not street:
        return (None, None)
    m = re.match(r"(\d+)\s+(\w+)", street)
    return (m.group(1), m.group(2)) if m else (None, None)


def is_owner_occupant(prop_address: str, prop_zip: str, raw_mailing: str) -> bool:
    """Determine if owner lives at the property based on mailing address.

    Key signal: if mailing street number + name matches property address,
    the owner lives there. Zips can differ (delivery vs actual zip).
    """
    parsed = parse_mailing_address(raw_mailing)
    if not parsed or not parsed.get("street"):
        return False

    mail_street = parsed.get("street", "")

    # PO box → not occupant
    if re.match(r"P\.?\s*O\.?\s*BOX", mail_street):
        return False

    # Compare leading number + street name
    pn, ps = leading_address(prop_address.upper())
    mn, ms = leading_address(mail_street)

    # If mailing address has no parseable street number, assume not occupant
    if not mn or not pn:
        return False

    # Street number + name must match
    if pn == mn and ps == ms:
        return True

    return False


def main():
    conn = psycopg2.connect(db_dsn())
    try:
        with conn.cursor() as cur:
            # Fetch all properties with mailing addresses
            cur.execute("""
                SELECT id, address, postal_code, owner_mailing_address
                FROM properties
                WHERE owner_mailing_address IS NOT NULL
            """)
            rows = cur.fetchall()
            print(f"Processing {len(rows)} properties with mailing addresses...")

            updated = 0
            for row_id, addr, zipcode, mailing in rows:
                occupant = is_owner_occupant(addr, zipcode, mailing)
                cur.execute(
                    "UPDATE properties SET owner_occupant = %s, updated_at = NOW() WHERE id = %s",
                    (occupant, row_id),
                )
                if occupant:
                    updated += 1

            conn.commit()
            print(f"Updated {len(rows)} properties, {updated} marked as owner-occupied")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
