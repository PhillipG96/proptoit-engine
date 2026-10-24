#!/usr/bin/env python3
"""Filter OSM streets for assessor scraping."""
import json, os, re, logging

log = logging.getLogger(__name__)
INPUT = os.path.expanduser("~/proptoit/data/streets.json")
OUTPUT = os.path.expanduser("~/proptoit/data/streets_filtered.json")

SUFFIXES = {"AVE","AVENUE","ST","STREET","RD","ROAD","BLVD","BOULEVARD","PL","PLACE",
    "CT","COURT","DR","DRIVE","LN","LANE","TER","WAY","PIKE","HWY","HIGHWAY",
    "PKWY","PARKWAY","LOOP","TRCE","TRACE","CLR","CIRCLE","CTR","CENTER",
    "HVN","MEWS","FLS","VW","HTS","HILL","CREST","GROVE","PARK","PORT",
    "SUMMIT","VLG","WY","BRG","HBR","JCT","KY","ML","RST","SHRS","STN",
    "TRWY","TRL","VIA","PATH","TRAIL","ALLEY","RAMP","SERVICE","DRIVEWAY",
    "GREENWAY","COMMONS","MEDIAN","ESPLANADE"}

EXCLUDE = {"TRAIL","PATH","ALLEY","SERVICE","RAMP","GREENWAY","COMMONS","MEDIAN","DRIVEWAY","ESPLANADE"}

def base_name(name):
    parts = name.upper().split()
    while parts and parts[-1] in SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts) if parts else name

def valid(name):
    u = name.upper()
    for e in EXCLUDE:
        if e in u: return False
    # Skip numeric-only or house-number-like names
    if re.match(r"^\d+$", name): return False
    if re.match(r"^\d+\s", name): return False  # "3216 ROSEMONT"
    if len(name) < 2: return False
    return True

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    with open(INPUT) as f: data = json.load(f)
    raw = data.get("streets", data if isinstance(data, list) else [])
    log.info("Loaded %d streets", len(raw))
    seen = set()
    filtered = []
    for name in raw:
        bn = base_name(name)
        if not valid(bn): continue
        if bn in seen: continue
        seen.add(bn)
        filtered.append(bn)
    log.info("Filtered to %d unique base names", len(filtered))
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump({"source": "OSM filtered", "count": len(filtered), "streets": filtered}, f, indent=2)
    log.info("Saved to %s", OUTPUT)
    print(f"\nBefore: {len(raw)} → After: {len(filtered)}")
    print(f"Sample: {filtered[:15]}")

if __name__ == "__main__":
    main()
