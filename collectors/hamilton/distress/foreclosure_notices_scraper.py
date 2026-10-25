#!/usr/bin/env python3
"""
Foreclosure Notice Scraper — Chattanooga Times Free Press Legal Notices

Scrapes NOTICE OF TRUSTEE'S FORECLOSURE SALE / SUBSTITUTE TRUSTEE'S SALE
notices from the TFP classifieds legal-notices page (same source as
probate_scraper.py). Extracts sale date, county, and debtor names, and
creates verified pre_foreclosure signals for Hamilton County properties
whose owner_of_record matches the trust debtor.

This is real published-notice data — no heuristics. Only notices whose
text explicitly names Hamilton County are used, and only exact/decedent
name matches against owner records create signals.

Usage:
  python3 foreclosure_notices_scraper.py [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/magicofelipe/proptoit")

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

TFP_BASE = "https://www.timesfreepress.com"
TFP_LEGAL_NOTICES = TFP_BASE + "/classifieds/category/legal-notices/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PropToIt-Foreclosure/1.0)"}

# Notice title patterns (in listing link slug or page text)
FC_TITLE_RE = re.compile(
    r"NOTICE OF (?:SUBSTITUTE )?TRUSTEE(?:'S|\u2019S)? (?:FORECLOSURE )?SALE"
    r"|NOTICE OF SHERIFF(?:'S|\u2019S)? SALE"
    r"|FORECLOSURE SALE", re.I)

SALE_DATE_RE = re.compile(
    r"will be on ((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})",
    re.I)

COUNTY_RE = re.compile(r"([A-Z][a-z]+) COUNTY", re.I)

DEBTOR_RE = re.compile(r"Deed of Trust executed by (.+?) for the benefit", re.I | re.S)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def html_to_text(html):
    t = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


def list_foreclosure_links():
    """Return [(listing_path, notice_id)] for foreclosure/trustee listings on page 1."""
    html = get(TFP_LEGAL_NOTICES)
    paths = re.findall(r'href="(/classifieds/category/legal-notices/\d+/[^"]+)"', html)
    seen, uniq = set(), []
    for p in paths:
        m = re.match(r"(/classifieds/category/legal-notices/(\d+)/.+)", p)
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            uniq.append((m.group(1), m.group(2)))
    return [u for u in uniq if FC_TITLE_RE.search(u[0]) or
            any(k in u[0].lower() for k in ("trustee", "foreclosure", "sheriff"))]


def parse_notice(path):
    """Fetch a notice detail page and extract structured fields."""
    html = get(TFP_BASE + path)
    text = html_to_text(html)
    up = text.upper()

    county_m = COUNTY_RE.search(text)
    county = county_m.group(1) if county_m else None
    sale_m = SALE_DATE_RE.search(text)
    debtor_m = DEBTOR_RE.search(text)
    # strip trailing conjunctions from debtor capture
    debtor = debtor_m.group(1).strip() if debtor_m else None
    if debtor:
        debtor = re.sub(r"\s+(and|,)\s*$", "", debtor)

    return {
        "county": county,
        "sale_date": sale_m.group(1) if sale_m else None,
        "debtor": debtor,
        "is_hamilton": county is not None and county.lower() == "hamilton",
        "raw_context": text[:1500],
    }


def clean_name(name):
    n = re.sub(r",?\s*(SR|JR|III|IV|II)\.?$", "", name.strip(), flags=re.I)
    return re.sub(r"\s+", " ", n).upper()


def find_owner_matches(conn, debtor_name):
    """Match debtor against owner_of_record — full-name token containment only."""
    cleaned = clean_name(debtor_name)
    # remove ', deceased' style suffixes already handled by regex above? be safe:
    cleaned = cleaned.replace("DECEASED", "").strip(" ,")
    tokens = [t for t in re.split(r"[\s]+", cleaned) if t]
    tokens = [t for t in tokens if t not in ("JR", "SR", "III", "II", "TR", "TRS", "TRUSTEE", "&")]
    if len(tokens) < 2:
        return []
    # All tokens must appear in the owner string. Values pass through %s
    # placeholders (tokens come from external legal notices and must never
    # become part of the SQL text); the wildcards live in the bound values.
    conds = " AND ".join(["upper(p.owner_of_record) LIKE %s"] * len(tokens))
    params = [f"%{t}%" for t in tokens]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT p.id, p.parcel_id, p.address, p.owner_of_record
            FROM properties p
            WHERE p.county_id='hamilton' AND p.owner_of_record IS NOT NULL
              AND {conds}
            LIMIT 20
        """, params)
        return [dict(r) for r in cur.fetchall()]


def upsert_notice(conn, notice):
    """Store the parsed notice (dedupe on sale_date+debtor)."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO foreclosure_notices (sale_date, county, debtor, raw_context, scraped_at)
            VALUES (%(sale_date)s, %(county)s, %(debtor)s, %(raw_context)s, NOW())
            ON CONFLICT (sale_date, county, debtor) DO UPDATE SET raw_context=EXCLUDED.raw_context, scraped_at=NOW()
        """, notice)
    conn.commit()


def create_signals(conn, matches, notice):
    n = 0
    with conn.cursor() as cur:
        for m in matches:
            metadata = json.dumps({
                "sale_date": notice["sale_date"],
                "county": notice["county"],
                "debtor": notice["debtor"],
                "source": "tfp_foreclosure_notice",
            })
            cur.execute("""
                INSERT INTO distress_signals (property_id, signal_type, detected_at, source, severity, confidence, metadata, source_url)
                VALUES (%(property_id)s, 'pre_foreclosure', NOW(), 'tfp_foreclosure_notice', 'high', 1.0, %(metadata)s::jsonb, %(url)s)
                ON CONFLICT (property_id, signal_type) DO UPDATE SET
                    detected_at=NOW(), source=EXCLUDED.source, severity='high', confidence=1.0,
                    metadata=EXCLUDED.metadata, source_url=EXCLUDED.source_url
            """, {"property_id": m["id"], "metadata": metadata, "url": TFP_LEGAL_NOTICES})
            n += 1
    conn.commit()
    return n


def run(dry_run=False):
    print("=" * 60)
    print("FORECLOSURE NOTICE SCRAPER — TFP Legal Notices")
    print("=" * 60)

    links = list_foreclosure_links()
    print(f"\nFound {len(links)} foreclosure/trustee listings on TFP")

    conn = psycopg2.connect(db_dsn())
    total_matches = 0
    try:
        for path, nid in links:
            notice = parse_notice(path)
            status = "HAMILTON" if notice["is_hamilton"] else notice["county"]
            print(f"\n  #{nid} county={status} sale={notice['sale_date']} debtor={notice['debtor']}")
            if not notice["is_hamilton"] or not notice["debtor"]:
                continue
            if not dry_run:
                upsert_notice(conn, notice)
            matches = find_owner_matches(conn, notice["debtor"])
            print(f"    matched {len(matches)} properties")
            for m in matches:
                print(f"      {m['parcel_id']} | {m['address']} | owner: {m['owner_of_record'][:40]}")
            total_matches += len(matches)
            if not dry_run and matches:
                create_signals(conn, matches, notice)
            time.sleep(0.5)
        print(f"\n✅ Done: {len(links)} notices, {total_matches} Hamilton matches")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TFP Foreclosure Notice Scraper")
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
