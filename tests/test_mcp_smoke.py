"""Smoke tests for the 16 free MCP tools (api/mcp_server.py) against the
live sample stack.

The whole module skips when the sample PostgreSQL (:5437) is unreachable, so
the suite stays green on machines without the stack. The Meilisearch-backed
tool additionally requires MEILI_MASTER_KEY. save_agent_memory is the one
write tool — its test cleans up after itself.
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("psycopg2")

import psycopg2  # noqa: E402


def _dsn() -> str:
    """Explicit env DATABASE_URL, else the sample-stack DSN (password from
    the repo-root .env that provisions the dockerized sample postgres)."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    pw = os.environ.get("POSTGRES_PASSWORD")
    if pw:
        from urllib.parse import quote_plus
        return f"postgresql://proptoit:{quote_plus(pw)}@localhost:5437/proptoit"
    return "postgresql://proptoit@localhost:5437/proptoit"


DSN = _dsn()
# The tools resolve DATABASE_URL at call time — make sure it's exported.
os.environ["DATABASE_URL"] = DSN


def _db_reachable() -> bool:
    try:
        psycopg2.connect(DSN, connect_timeout=3).close()
        return True
    except Exception:
        return False


DB_OK = _db_reachable()
pytestmark = pytest.mark.skipif(not DB_OK, reason="sample stack not reachable")

if DB_OK:
    import mcp_server  # noqa: E402


def _parse(raw: str):
    """Every tool returns a JSON string; decode it once, centrally."""
    return json.loads(raw)


def _assert_no_license_error(data) -> None:
    if isinstance(data, dict):
        assert data.get("error") != "license_required"


def _first_parcel() -> str:
    conn = psycopg2.connect(DSN, connect_timeout=3)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT parcel_id FROM properties "
            "WHERE county_id = 'hamilton' AND parcel_id IS NOT NULL LIMIT 1")
        row = cur.fetchone()
    conn.close()
    assert row, "sample database has no parcels"
    return row[0]


def _two_parcels() -> list:
    conn = psycopg2.connect(DSN, connect_timeout=3)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT parcel_id FROM properties "
            "WHERE county_id = 'hamilton' AND parcel_id IS NOT NULL LIMIT 2")
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 2, "need at least two parcels to compare"
    return [r[0] for r in rows]


# ── free tools (one smoke test each) ──────────────────────────────

def test_search_properties():
    data = _parse(mcp_server.search_properties(query="oak", limit=5))
    _assert_no_license_error(data)


def test_get_property_detail():
    data = _parse(mcp_server.get_property_detail(_first_parcel()))
    _assert_no_license_error(data)


def test_list_foreclosures():
    _assert_no_license_error(_parse(mcp_server.list_foreclosures(limit=10)))


def test_get_distress_signals():
    _assert_no_license_error(_parse(mcp_server.get_distress_signals(_first_parcel())))


def test_list_fsbo():
    _assert_no_license_error(_parse(mcp_server.list_fsbo(limit=5)))


def test_assemble_context():
    _assert_no_license_error(_parse(mcp_server.assemble_context(parcel_id=_first_parcel())))


def test_compare_properties():
    data = _parse(mcp_server.compare_properties(parcel_ids=_two_parcels()))
    _assert_no_license_error(data)


def test_list_distressed_with_coords():
    _assert_no_license_error(_parse(mcp_server.list_distressed_with_coords(limit=5)))


def test_get_system_stats():
    data = _parse(mcp_server.get_system_stats())
    _assert_no_license_error(data)


def test_search_by_category():
    _assert_no_license_error(
        _parse(mcp_server.search_by_category(category="residential", limit=5)))


def test_get_foreclosure_stats():
    _assert_no_license_error(_parse(mcp_server.get_foreclosure_stats()))


def test_get_market_context():
    _assert_no_license_error(_parse(mcp_server.get_market_context()))


def test_get_comps():
    _assert_no_license_error(_parse(mcp_server.get_comps(_first_parcel())))


def test_list_counties():
    data = _parse(mcp_server.list_counties())
    _assert_no_license_error(data)


def test_meilisearch_search():
    if not os.environ.get("MEILI_MASTER_KEY"):
        pytest.skip("MEILI_MASTER_KEY not set")
    _assert_no_license_error(_parse(mcp_server.meilisearch_search(query="oak")))


def test_save_agent_memory_cleans_up():
    """The one write tool: save a note, confirm it parses, then delete it."""
    out = _parse(mcp_server.save_agent_memory(
        parcel_id="SMOKE-TEST", content="test note — safe to delete"))
    _assert_no_license_error(out)
    conn = psycopg2.connect(DSN, connect_timeout=3)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_memory WHERE parcel_id = 'SMOKE-TEST'")
        deleted = cur.rowcount
        conn.commit()
    conn.close()
    assert deleted >= 1
