"""PropToIt Pro — license-keyed extensions (deal scoring, investor tools, triage).

This package is physically separated from the free core (api/) so it can be
distributed under a commercial license. No license-gating logic is implemented
in the open-source phase; these modules simply do not ship in the OSS build.

Extra dependencies beyond requirements.txt:
    PyJWT        (pro/investor_router.py — investor access tokens)

Note: pro modules import from the api package (`from api.sanitize import ...`,
`from api.core.matching import ...`), so run from the repository root or ensure
it is on sys.path.
"""
