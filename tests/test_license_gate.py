"""Task 2.2 gate suite: the pro MCP tools are licensed, fail-closed, call-time.

Contract under test (api/mcp_server._pro_license_gate + pro.require_license):
  - no key / garbage key -> every pro tool returns a license_required error
    result carrying the exact brief message; the tool body never runs
  - a valid key (hermetic Ed25519 test key: embedded pubkey monkeypatched to
    the deterministic TEST_SEED pubkey, exactly like tests/test_license.py)
    -> the tool body executes
  - PROPTOIT_LICENSE_KEY is read at CALL time (setting it after import works,
    unsetting after import denies) — never at import time
  - KeyInfo is always truthy: the gate checks .valid, never the object
  - a real-key subprocess test signs via scripts/license_keygen.py and skips
    when this machine's private key is absent (CI-safe; the private key is
    never read by the test itself)

Everything here is hermetic (no DB, no Meilisearch, no network): the gate
returns before any tool body runs, and the valid-key tests stub the tool
bodies' data functions.
"""

import base64
import inspect
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import api.mcp_server as srv  # noqa: E402
from pro import PRO_LICENSE_MESSAGE, require_license  # noqa: E402
from pro import license as lic  # noqa: E402
from pro import matching as pro_matching  # noqa: E402

# The four gated pro tools, invoked with minimal-arg calls that would hit
# live data if (incorrectly) ungated.
PRO_TOOLS = {
    "score_leads": lambda: srv.score_leads(max_total_investment=300000),
    "get_deal_analysis": lambda: srv.get_deal_analysis(parcel_id="0000000A00001"),
    "get_top_deals": lambda: srv.get_top_deals(limit=5),
    "get_arv": lambda: srv.get_arv(parcel_id="0000000A00001"),
}

# Captured at import, BEFORE any fixture monkeypatches it.
TEST_SEED = bytes(range(32))
MACHINE_KEY_PATH = Path.home() / ".hermes" / "secrets" / "proptoit-engine" / "license_ed25519.key"


def _pub_hex(seed: bytes) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


TEST_PUB_HEX = _pub_hex(TEST_SEED)


@pytest.fixture(autouse=True)
def _no_license_env(monkeypatch):
    """Start every test with NO license key in the environment.

    Also neutralizes any PROPTOIT_LICENSE_KEY that .env / load_dotenv might
    have injected when api.mcp_server was imported.
    """
    monkeypatch.delenv("PROPTOIT_LICENSE_KEY", raising=False)


@pytest.fixture
def _test_pubkey(monkeypatch):
    """Point the verifier at the deterministic test public key."""
    monkeypatch.setattr(lic, "_ED25519_PUB_HEX", TEST_PUB_HEX)


def _sign(
    tier: str = "personal",
    email: str = "gate-test@example.com",
    exp: Optional[str] = None,
) -> str:
    """Sign a license payload with the deterministic TEST_SEED key."""
    priv = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    payload = {"tier": tier, "email": email, "major_version": lic.ENGINE_MAJOR_VERSION}
    if exp:
        payload["exp"] = exp
    msg = lic.canonical_payload_bytes(payload)
    return lic.pack_license(msg, priv.sign(msg))


def _assert_license_required(result: str) -> dict:
    data = json.loads(result)
    assert data["error"] == "license_required"
    assert data["message"] == PRO_LICENSE_MESSAGE
    # The exact message from the Task 2.2 brief, verbatim.
    assert data["message"] == "Pro license required — proptoit.com/pricing"
    return data


# ------------------------------------------------------------------ denial


@pytest.mark.parametrize("tool_name", sorted(PRO_TOOLS))
def test_no_key_returns_license_required(tool_name):
    """No PROPTOIT_LICENSE_KEY -> clean error result, not a crash."""
    _assert_license_required(PRO_TOOLS[tool_name]())


@pytest.mark.parametrize("tool_name", sorted(PRO_TOOLS))
def test_garbage_key_returns_license_required(tool_name, monkeypatch):
    """A present-but-invalid key -> same clean error result, with a reason."""
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", "not-a-real-key")
    data = _assert_license_required(PRO_TOOLS[tool_name]())
    assert data["detail"]  # verifier reason surfaced, no pro data leaked


@pytest.mark.parametrize("tool_name", sorted(PRO_TOOLS))
def test_malformed_key_returns_license_required(tool_name, monkeypatch):
    """Plausible-shaped but bogus key -> still denied."""
    # Same shape as a real license, both segments decode to "nope" — built at
    # runtime so a fake token literal never sits in the source (gitleaks-clean).
    _nope = base64.b64encode(b"nope").decode()
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", f"PTOIT1.{_nope}.{_nope}")
    _assert_license_required(PRO_TOOLS[tool_name]())


def test_unkeyed_call_never_runs_tool_body(monkeypatch):
    """Fail-closed: the tool body is unreachable without a license."""

    def _bomb(*args, **kwargs):
        raise AssertionError("tool body must not run without a license")

    monkeypatch.setattr(pro_matching, "score_leads", _bomb)
    _assert_license_required(srv.score_leads(max_total_investment=300000))


def test_wrong_version_key_is_denied(_test_pubkey, monkeypatch):
    """ENGINE_MAJOR_VERSION pin applies to pro tools too (contract from 2.1)."""
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", _sign())
    # Re-sign with a wrong major version by tampering the payload — simplest:
    # build a key for major_version + 1.
    priv = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    payload = {
        "tier": "personal",
        "email": "gate-test@example.com",
        "major_version": lic.ENGINE_MAJOR_VERSION + 1,
    }
    msg = lic.canonical_payload_bytes(payload)
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", lic.pack_license(msg, priv.sign(msg)))
    _assert_license_required(srv.get_top_deals(limit=5))


# ------------------------------------------------------------- valid access


def test_valid_key_executes_tool_body(_test_pubkey, monkeypatch):
    """With a valid key the gate opens and the tool body executes."""
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", _sign())
    deals = [{"parcelId": "0101200A00105", "address": "1 Test St", "match_score": 91.5}]
    monkeypatch.setattr(pro_matching, "score_leads", lambda profile: deals)
    data = json.loads(srv.score_leads(max_total_investment=300000))
    assert data["count"] == 1
    assert data["deals"] == deals
    assert "error" not in data


def test_valid_platform_key_executes_tool_body(_test_pubkey, monkeypatch):
    """platform tier works the same way (verify() accepts both tiers)."""
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", _sign(tier="platform"))
    analysis = {"parcelId": "0101200A00105", "equity": 42000}
    monkeypatch.setattr(srv.queries, "get_property_analysis", lambda *a, **k: analysis)
    data = json.loads(srv.get_deal_analysis(parcel_id="0101200A00105"))
    assert data == analysis


def test_valid_key_real_score_leads_against_live_db(_test_pubkey, monkeypatch):
    """With a valid key, score_leads runs the real engine; if a live DB is
    configured it must return real data, and if not the gate must still be
    open (a DB connection error is an operational error, NOT a license
    denial)."""
    if not __import__("os").getenv("DATABASE_URL"):
        pytest.skip("no DATABASE_URL — gate-open is asserted by the stubbed test above")
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", _sign())
    result = json.loads(srv.score_leads(max_total_investment=5_000_000))
    assert "error" not in result
    assert "count" in result and "deals" in result


def test_env_read_at_call_time_not_import_time(_test_pubkey, monkeypatch):
    """api.mcp_server was already imported at the top of this module — the
    gate still consults the env on every call."""
    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", _sign())
    assert require_license().valid is True

    monkeypatch.delenv("PROPTOIT_LICENSE_KEY")
    info = require_license()
    assert info.valid is False

    monkeypatch.setenv("PROPTOIT_LICENSE_KEY", "garbage-after-import")
    assert require_license().valid is False


def test_keyinfo_always_truthy_gate_on_valid_flag():
    """Task 2.1 review carry-forward: KeyInfo is ALWAYS truthy (dataclass).
    Gating on the object instead of .valid would grant everyone access."""
    from pro.license import KeyInfo

    bad = KeyInfo(valid=False, reason="no license key set (env PROPTOIT_LICENSE_KEY)")
    assert bad  # truthy object…
    assert bad.valid is False  # …so the gate MUST check .valid
    assert srv._pro_license_gate() is not None  # and _pro_license_gate does


def test_free_tools_are_not_gated():
    """Free core tools stay untouched — no gate in their bodies."""
    src = inspect.getsource(srv.search_properties)
    assert "_pro_license_gate" not in src
    assert "require_license" not in src


def test_all_pro_tools_registered():
    """The 4 pro tools are discoverable (gated, not hidden)."""
    import asyncio

    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    assert {"score_leads", "get_deal_analysis", "get_top_deals", "get_arv"} <= names


# ------------------------------------------------- real keychain (optional)


@pytest.mark.skipif(
    not MACHINE_KEY_PATH.exists(),
    reason="machine signing key not present (CI-safe)",
)
def test_real_keygen_signs_a_key_the_verifier_accepts():
    """Full real-keychain check: scripts/license_keygen.py signs with the
    machine's private key; pro.license.verify accepts it against the real
    embedded public key. The test never reads the private key itself."""
    out = subprocess.run(
        [sys.executable, "scripts/license_keygen.py", "sign",
         "--tier", "personal", "--email", "gate-e2e@example.com"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    key = out.stdout.strip().splitlines()[-1]
    assert key.startswith("PTOIT1.")
    info = lic.verify(key)  # real pubkey — deliberately no monkeypatch here
    assert info.valid is True
    assert info.tier == "personal"
