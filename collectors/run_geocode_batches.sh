#!/bin/bash
# Geocode properties missing coordinates via the consolidated geocoder.
# Census batch handles numbered addresses in bulk; Nominatim fills the rest at
# its rate limit (no external CSV batching/pauses needed here anymore).
# Usage: bash run_geocode_batches.sh [county] [target]
set -euo pipefail

cd ~/proptoit

COUNTY="${1:-hamilton}"
TARGET="${2:-missing}"

python3 -m collectors.geocoding --strategy census --county "$COUNTY" --target "$TARGET"
