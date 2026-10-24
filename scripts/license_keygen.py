#!/usr/bin/env python3
"""Ed25519 license keygen + offline signer for PropToIt Pro.

Subcommands
    keygen  Generate an Ed25519 keypair. The PRIVATE key is written to
            --out-dir (default: ~/.hermes/secrets/proptoit-engine/ — outside
            any git repository) with mode 0600. NEVER commit it. The public
            key is printed; it is public and gets pasted into pro/license.py
            (_ED25519_PUB_HEX).
    sign    Sign a license payload {tier, email, major_version, exp?} with
            the private key and print the license key string.

Examples
    python scripts/license_keygen.py keygen
    python scripts/license_keygen.py sign --tier personal --email me@x.io --exp 2027-12-31

No network access anywhere; the private key never leaves this machine.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from pro.license import (  # noqa: E402
    ENGINE_MAJOR_VERSION,
    LICENSE_TIERS,
    canonical_payload_bytes,
    pack_license,
)

DEFAULT_KEY_DIR = Path.home() / ".hermes" / "secrets" / "proptoit-engine"
KEY_FILENAME = "license_ed25519.key"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _write_private_key(path: Path, seed: bytes, public_hex: str, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing private key {path} (use --force)")
    path.parent.mkdir(parents=True, exist_ok=True)
    keydata = {
        "algorithm": "Ed25519",
        "seed_hex": seed.hex(),
        "public_hex": public_hex,
        "created": date.today().isoformat(),
        "note": "PRIVATE KEY — never commit, never copy into the repo",
    }
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(keydata, fh, indent=2)
        fh.write("\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    path.chmod(0o600)


def cmd_keygen(args: argparse.Namespace) -> int:
    key_path = Path(args.out_dir).expanduser() / KEY_FILENAME
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    _write_private_key(key_path, seed, public_hex, args.force)
    print(f"private key: {key_path}  (mode 0600 — NEVER commit)")
    print(f"public key : {public_hex}")
    print("paste the public key into pro/license.py (_ED25519_PUB_HEX)")
    return 0


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not path.is_file():
        raise SystemExit(f"private key not found: {path} (run the 'keygen' subcommand first)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("algorithm") != "Ed25519" or not data.get("seed_hex"):
        raise SystemExit(f"{path} is not a recognized Ed25519 key file")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["seed_hex"]))


def cmd_sign(args: argparse.Namespace) -> int:
    if args.exp is not None:
        if not _DATE_RE.match(args.exp):
            raise SystemExit("--exp must be an ISO date (YYYY-MM-DD)")
        try:
            date.fromisoformat(args.exp)
        except ValueError:
            raise SystemExit("--exp is not a valid date") from None
    email = args.email.strip()
    if "@" not in email or not 3 <= len(email) <= 254:
        raise SystemExit("--email must be a valid address")

    payload = {"tier": args.tier, "email": email, "major_version": args.major_version}
    if args.exp is not None:
        payload["exp"] = args.exp

    priv = _load_private_key(Path(args.key).expanduser())
    msg = canonical_payload_bytes(payload)
    print(pack_license(msg, priv.sign(msg)))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    kg = sub.add_parser("keygen", help="generate an Ed25519 keypair (private key stays local)")
    kg.add_argument("--out-dir", default=str(DEFAULT_KEY_DIR),
                    help="private-key directory (default: %(default)s)")
    kg.add_argument("--force", action="store_true", help="overwrite an existing private key")
    kg.set_defaults(func=cmd_keygen)

    sg = sub.add_parser("sign", help="sign a license payload; prints the license key")
    sg.add_argument("--tier", required=True, choices=sorted(LICENSE_TIERS))
    sg.add_argument("--email", required=True)
    sg.add_argument("--major-version", type=int, default=ENGINE_MAJOR_VERSION,
                    help=f"engine major version to pin (default: {ENGINE_MAJOR_VERSION})")
    sg.add_argument("--exp", default=None, help="optional expiry YYYY-MM-DD")
    sg.add_argument("--key", default=str(DEFAULT_KEY_DIR / KEY_FILENAME),
                    help="private key path (default: %(default)s)")
    sg.set_defaults(func=cmd_sign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
