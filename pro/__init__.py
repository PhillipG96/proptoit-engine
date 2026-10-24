"""PropToIt Pro — license-keyed extensions (deal scoring, investor tools, triage).

This package is physically separated from the free core (api/) so it can be
distributed under a commercial license. It SHIPS in this repository but is
GATED: before any pro data leaves the process, the caller must present a
valid offline Ed25519 license (pro/license.py).

Gate contract (Task 2.2)::

    from pro import require_license

    info = require_license()   # reads PROPTOIT_LICENSE_KEY at CALL time
    if not info.valid:         # gate on .valid — NEVER on truthiness
        ...                    # deny: KeyInfo object is always truthy

Extra dependencies beyond requirements.txt:
    PyJWT        (pro/investor_router.py — investor access tokens)

Note: pro modules import from the api package (`from api.sanitize import ...`,
`from api.core.queries import ...`), so run from the repository root or ensure
it is on sys.path.
"""

import os

from .license import KeyInfo, verify

#: Environment variable holding the license key. Read at CALL time — never at
#: import time — matching the repo-wide rule against import-time env reads.
LICENSE_ENV_VAR = "PROPTOIT_LICENSE_KEY"

#: The exact message unlicensed pro tool calls return (no pro data, no crash).
PRO_LICENSE_MESSAGE = "Pro license required — proptoit.com/pricing"


def require_license() -> KeyInfo:
    """Verify the license key from ``LICENSE_ENV_VAR`` — offline, fail-closed.

    Reads the env var at CALL time (not import time), so operators can set or
    rotate the key without restarting the process and so a missing key is a
    clean denial rather than an import error.

    Returns a ``KeyInfo`` in every case and never raises; callers gate on
    ``info.valid`` — NEVER on truthiness of the KeyInfo itself (a dataclass
    instance is always truthy by construction).
    """
    key = os.getenv(LICENSE_ENV_VAR) or ""
    if not key.strip():
        return KeyInfo(
            valid=False,
            reason=f"no license key set (env {LICENSE_ENV_VAR})",
        )
    return verify(key)
