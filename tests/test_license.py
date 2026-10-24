"""Task 2.1 TDD suite for the offline Ed25519 license verifier (pro/license.py).

Brief-required cases: expired key, wrong-version key, tampered key, valid
personal key, valid platform key — plus malformed/fail-closed coverage and a
real subprocess end-to-end run of scripts/license_keygen.py.

The suite is fully deterministic: fixed Ed25519 seeds, and the embedded public
key is monkeypatched to the matching test public key. No key material is read
from disk, and nothing here touches the real signing key.
"""

import inspect
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from pro import license as lic  # noqa: E402

# Captured BEFORE the autouse fixture swaps it: the real embedded pubkey.
REAL_PUB_HEX = lic._ED25519_PUB_HEX

TEST_SEED = bytes(range(32))
OTHER_SEED = bytes(reversed(range(32)))


def _pub_hex(seed: bytes) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


TEST_PUB_HEX = _pub_hex(TEST_SEED)
OTHER_PUB_HEX = _pub_hex(OTHER_SEED)


@pytest.fixture(autouse=True)
def _use_test_pubkey(monkeypatch):
    """Point the verifier at the deterministic test public key."""
    monkeypatch.setattr(lic, "_ED25519_PUB_HEX", TEST_PUB_HEX)


def _sign(payload: dict, seed: bytes = TEST_SEED) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    msg = lic.canonical_payload_bytes(payload)
    return lic.pack_license(msg, priv.sign(msg))


def _payload(**over):
    p = {"tier": "personal", "email": "dev@example.com", "major_version": lic.ENGINE_MAJOR_VERSION}
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


FUTURE = (date.today() + timedelta(days=365)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


def _sig64() -> str:
    return lic.b64url_encode(b"\x00" * 64)


def _payload_b64(obj) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return lic.b64url_encode(raw)


# ---------------------------------------------------------------- valid keys

def test_valid_personal_key():
    info = lic.verify(_sign(_payload(tier="personal")))
    assert isinstance(info, lic.KeyInfo)
    assert info.valid is True
    assert info.tier == "personal"
    assert info.email == "dev@example.com"
    assert info.major_version == lic.ENGINE_MAJOR_VERSION
    assert info.reason == "ok"


def test_valid_platform_key():
    info = lic.verify(_sign(_payload(tier="platform", exp=FUTURE)))
    assert info.valid is True
    assert info.tier == "platform"
    assert info.exp == FUTURE
    assert info.reason == "ok"


def test_valid_key_without_exp():
    info = lic.verify(_sign(_payload()))
    assert info.valid is True
    assert info.exp == ""


def test_signature_checked_against_exact_canonical_bytes():
    """Same payload dict, different key order — must verify identically."""
    payload = _payload(tier="platform")
    priv = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    for order in ({"major_version": payload["major_version"], "email": payload["email"], "tier": "platform"}, payload):
        msg = json.dumps(order, sort_keys=True, separators=(",", ":")).encode()
        assert lic.verify(lic.pack_license(msg, priv.sign(msg))).valid is True


# ------------------------------------------------------- brief-required negs

def test_expired_key():
    info = lic.verify(_sign(_payload(exp=PAST)))
    assert info.valid is False
    assert "expired" in info.reason


def test_wrong_version_key():
    info = lic.verify(_sign(_payload(major_version=lic.ENGINE_MAJOR_VERSION + 7)))
    assert info.valid is False
    assert "version" in info.reason


def test_tampered_payload_byte():
    key_str = _sign(_payload(exp=FUTURE))
    prefix, payload_b64, sig_b64 = key_str.split(".")
    raw = bytearray(lic.b64url_decode(payload_b64))
    raw[0] ^= 0x01
    info = lic.verify(f"{prefix}.{lic.b64url_encode(bytes(raw))}.{sig_b64}")
    assert info.valid is False


def test_tampered_signature_byte():
    key_str = _sign(_payload())
    prefix, payload_b64, sig_b64 = key_str.split(".")
    raw = bytearray(lic.b64url_decode(sig_b64))
    raw[0] ^= 0x01
    info = lic.verify(f"{prefix}.{payload_b64}.{lic.b64url_encode(bytes(raw))}")
    assert info.valid is False
    assert "signature" in info.reason


def test_wrong_public_key():
    """Signed by a different keypair than the one embedded in the verifier."""
    info = lic.verify(_sign(_payload(), seed=OTHER_SEED))
    assert info.valid is False
    assert "signature" in info.reason


# ------------------------------------------------- malformed / fail-closed

@pytest.mark.parametrize("s", [
    "", "   ", "garbage", "PTOIT1", "PTOIT1.a", "PTOIT1.a.b.c",
    "PTOIT1.!!!.???", "PTOIT2.abc.def",
    f"PTOIT1.{_payload_b64(b'not json')}.{_sig64()}",
    f"PTOIT1.{_payload_b64([1, 2])}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'email': 'a@b.c', 'major_version': 0})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'a@b.c', 'major_version': 0, 'nonsense': 1})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'enterprise', 'email': 'a@b.c', 'major_version': 0})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'nope', 'major_version': 0})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'a@b.c', 'major_version': True})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'a@b.c', 'major_version': '0'})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'a@b.c', 'major_version': 0, 'exp': '2026-13-99'})}.{_sig64()}",
    f"PTOIT1.{_payload_b64({'tier': 'personal', 'email': 'a@b.c', 'major_version': 0, 'exp': '20261024'})}.{_sig64()}",
])
def test_malformed_and_semantic_rejects(s):
    info = lic.verify(s)
    assert isinstance(info, lic.KeyInfo)
    assert info.valid is False
    assert info.reason  # always explains WHY


def test_verify_never_raises():
    for nasty in (None, 123, b"PTOIT1", ["x"], {}, "PTOIT1." + "A" * 5000,
                  f"PTOIT1.{'A=' * 40}.{_sig64()}"):
        info = lic.verify(nasty)
        assert isinstance(info, lic.KeyInfo)
        assert info.valid is False


def test_canonical_payload_is_deterministic():
    assert lic.canonical_payload_bytes({"b": 2, "a": 1}) == lic.canonical_payload_bytes({"a": 1, "b": 2})


def test_embedded_pubkey_is_wellformed():
    assert re.fullmatch(r"[0-9a-f]{64}", REAL_PUB_HEX), "embedded public key must be 64 lowercase hex chars"


def test_no_network_anywhere():
    """Brief: 'no network calls'. Static check over verifier + keygen source."""
    sources = []
    for rel in ("pro/license.py", "scripts/license_keygen.py"):
        sources.append((REPO_ROOT / rel).read_text(encoding="utf-8"))
    sources.append(inspect.getsource(lic))
    banned = re.compile(
        r"\b(import\s+(socket|requests|urllib|urllib2|http|httpx|telnetlib|ftplib)|"
        r"urlopen|socket\.socket|httpx\.)", re.MULTILINE,
    )
    for src in sources:
        assert not banned.search(src), "license code must make no network calls"


# ------------------------------------------- real keygen script (subprocess)

SCRIPT = REPO_ROOT / "scripts" / "license_keygen.py"


def _run(*argv):
    return subprocess.run([sys.executable, str(SCRIPT), *argv],
                          capture_output=True, text=True, cwd=str(REPO_ROOT))


def test_keygen_script_end_to_end(tmp_path):
    keydir = tmp_path / "keys"
    kg = _run("keygen", "--out-dir", str(keydir))
    assert kg.returncode == 0, kg.stderr
    keyfile = keydir / "license_ed25519.key"
    assert keyfile.is_file()
    assert (keyfile.stat().st_mode & 0o777) == 0o600, "private key must be 0600"
    keydata = json.loads(keyfile.read_text())
    assert re.fullmatch(r"[0-9a-f]{64}", keydata["seed_hex"])

    # Verify with the script's own public key (overrides the fixture value;
    # monkeypatch restores the real embedded key afterwards).
    lic._ED25519_PUB_HEX = keydata["public_hex"]
    sg = _run("sign", "--tier", "personal", "--email", "e2e@example.com",
              "--exp", FUTURE, "--key", str(keyfile))
    assert sg.returncode == 0, sg.stderr
    key_str = sg.stdout.strip().splitlines()[-1]
    assert key_str.startswith(lic.LICENSE_PREFIX + ".")
    info = lic.verify(key_str)
    assert info.valid is True
    assert info.tier == "personal" and info.exp == FUTURE

    # Keygen refuses to clobber an existing private key without --force.
    again = _run("keygen", "--out-dir", str(keydir))
    assert again.returncode != 0
    forced = _run("keygen", "--out-dir", str(keydir), "--force")
    assert forced.returncode == 0
    assert json.loads(keyfile.read_text())["public_hex"] != keydata["public_hex"]


def test_keygen_sign_rejects_bad_input(tmp_path):
    kg = _run("keygen", "--out-dir", str(tmp_path / "k"))
    assert kg.returncode == 0
    keyfile = tmp_path / "k" / "license_ed25519.key"

    bad_exp = _run("sign", "--tier", "personal", "--email", "x@y.z",
                   "--exp", "not-a-date", "--key", str(keyfile))
    assert bad_exp.returncode != 0
    bad_tier = _run("sign", "--tier", "enterprise", "--email", "x@y.z", "--key", str(keyfile))
    assert bad_tier.returncode != 0
    bad_email = _run("sign", "--tier", "personal", "--email", "nope", "--key", str(keyfile))
    assert bad_email.returncode != 0
    missing_key = _run("sign", "--tier", "personal", "--email", "x@y.z",
                       "--key", str(tmp_path / "nope.key"))
    assert missing_key.returncode != 0
