"""
License gate for pro HTTP endpoints.

FastAPI dependency mirroring how api/mcp_server.py gates the pro MCP tools:
the key is read from PROPTOIT_LICENSE_KEY at CALL time (never at import, so
it can be set or rotated without a restart) and verified fully offline via
pro/license.py. Fail-closed: a missing, malformed, tampered or expired key
denies with HTTP 403 and a ``license_required`` detail — the endpoint body
never runs, so no pro computation can leak over HTTP.
"""
import logging
import sys
from pathlib import Path

# pro/ lives at the repo root; make it importable whether this module is
# loaded as core.gate (api/ on sys.path) or api.core.gate (package import).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import HTTPException

from pro import require_license

logger = logging.getLogger(__name__)


def require_pro_license() -> None:
    """FastAPI dependency: deny with 403 unless a valid license is present.

    Gates on ``info.valid`` — never on truthiness of the KeyInfo itself (a
    dataclass instance is always truthy by construction). Missing, invalid
    and expired keys all land here, so the gate can never silently allow.
    """
    info = require_license()
    if not info.valid:
        logger.warning("Pro endpoint denied: %s", info.reason)
        raise HTTPException(status_code=403, detail="license_required")
