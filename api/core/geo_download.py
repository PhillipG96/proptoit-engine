#!/usr/bin/env python3
"""Download MaxMind GeoLite2 free database.
Requires an account at https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
"""
import os
import logging
import urllib.request
from pathlib import Path
logger = logging.getLogger(__name__)

DB_URL = "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&license_key={}&suffix=tar.gz"
DB_DIR = Path("/home/magicofelipe/proptoit/data/geo")
VALIDATE_URL = "https://secret-scanning.maxmind.com/secrets/validate-license-key"


def validate_key(key: str) -> bool:
    """Validate a MaxMind license key via the public secret-scanning API."""
    data = f"license_key={key}".encode()
    req = urllib.request.Request(
        VALIDATE_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 204
    except urllib.error.HTTPError as e:
        logger.info(f"Validation failed ({e.code}): {e.read().decode()}")
        return False
    except Exception as e:
        logger.info(f"Validation error: {e}")
        return False


def download(maxmind_key: str | None = None) -> str:
    """Download and extract GeoLite2 Country database."""
    key = maxmind_key or os.getenv("MAXMIND_LICENSE_KEY")
    if not key:
        logger.info("Set MAXMIND_LICENSE_KEY or pass it directly. Get free key at dev.maxmind.com")
        return ""
    if not validate_key(key):
        logger.info("License key is invalid. Aborting.")
        return ""
    URL = DB_URL.format(key)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = DB_DIR / "geo.tgz"
    logger.info(f"Downloading GeoLite2 from MaxMind...")
    urllib.request.urlretrieve(URL, tar_path)
    # Extract
    import tarfile
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            if member.name.endswith(".mmdb"):
                extract_path = DB_DIR / member.name.split("/")[-1]
                f = tar.extractfile(member)
                if f:
                    with open(extract_path, "wb") as out:
                        out.write(f.read())
                    logger.info(f"Extracted to {extract_path} ({extract_path.stat().st_size} bytes)")
                    return str(extract_path)
    return ""

if __name__ == "__main__":
    download()
