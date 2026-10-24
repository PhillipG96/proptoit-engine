#!/usr/bin/env python3
"""FSBO scan of Chattanooga Craigslist — parses JSON-LD structured data."""
import json
import re
import sys
from datetime import datetime
from urllib.request import urlopen, Request

URL = "https://chattanooga.craigslist.org/search/rea"

req = Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urlopen(req, timeout=15) as resp:
    html = resp.read().decode("utf-8")

# Extract JSON-LD from ld_searchpage_results
match = re.search(r'id="ld_searchpage_results"\s*>\s*(\{.*?\})\s*</script>', html, re.DOTALL)
if not match:
    print("ERROR: Could not find JSON-LD data")
    sys.exit(1)

data = json.loads(match.group(1))
items = data.get("itemListElement", [])

print(f"Total listings: {len(items)}")
print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
print()

# Flag investor-relevant signals
keywords = ["fsbo", "for sale by owner", "investor", "fixer", "cash", "owner finance",
             "below", "appraisal", "wholesale", "distress", "probate", "as-is",
             "handyman", "special", "motivated", "landlord", "rental", "multi",
             "seller finance", "seller finance", "off market"]

signals = []
for item in items:
    listing = item.get("item", {})
    name = listing.get("name", "")
    lower = name.lower()
    matched = [k for k in keywords if k in lower]
    if matched:
        signals.append({"title": name, "signals": matched})

print(f"Investor-relevant signals: {len(signals)}")
print()

for i, s in enumerate(signals, 1):
    print(f"  {i}. [{', '.join(s['signals'])}] {s['title']}")

print(f"\nAll listings ({len(items)}):")
for i, item in enumerate(items, 1):
    listing = item.get("item", {})
    name = listing.get("name", "(no title)")
    print(f"  {i}. {name}")
