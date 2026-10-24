"""PropToIt CLI — init, index, serve-mcp, triage.

Install from the repository root so the CLI can drive the api/ and collectors/
trees in place (this is the supported install mode)::

    pip install -e .

Subcommands
    init        Ensure .env exists with fresh random secrets. Never overwrites
                an existing .env (a live stack's secrets are never regenerated).
                Does NOT boot containers — that is `make dev`'s job.
    index       Reindex properties into Meilisearch via the real indexer
                (collectors/index_to_meili.py). DSN/Meili config is env-driven:
                explicit flag > environment > .env-derived sample stack.
    serve-mcp   Run the MCP server over stdio (api/mcp_server.py). This is the
                command a Claude Desktop / Claude Code config block points at::
                    {"mcpServers": {"proptoit": {"command": "proptoit",
                                                 "args": ["serve-mcp"]}}}
    triage      [Pro] Batch-load a Hamilton County assessor bulk CSV into
                skip-trace results (pro/triage). License-gated, fail-closed;
                DRY RUN by default — pass --commit to write.
"""
from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    """Repo root = parent of the proptoit package.

    Valid for the supported install mode (`pip install -e .` from the repo
    root, where proptoit/ sits beside api/ and collectors/). A copied
    (non-editable) install cannot drive those trees, so fail loudly instead
    of half-working.
    """
    root = Path(__file__).resolve().parents[1]
    if not (root / "api" / "mcp_server.py").exists():
        sys.exit(
            "proptoit CLI must be installed from the repository root: "
            "`pip install -e .` (it drives api/ and collectors/ in place)."
        )
    return root


def _load_env(root: Path) -> None:
    """Pull secrets/ports from the repo .env (gitignored) into the environment.

    Existing process environment wins — an operator-exported DATABASE_URL etc.
    is never overridden by .env.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - guarded by install deps
        sys.exit("python-dotenv missing — reinstall with `pip install -e .`")
    load_dotenv(root / ".env")


def _sample_dsn() -> str:
    """DSN of the local sample stack; password comes from .env, never code."""
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        sys.exit(
            "POSTGRES_PASSWORD missing — run `proptoit init` (or `make dev`) first."
        )
    port = os.environ.get("POSTGRES_HOST_PORT", "5437")
    return f"postgresql://proptoit:{pw}@localhost:{port}/proptoit"


def _sample_meili_url() -> str:
    port = os.environ.get("MEILI_HOST_PORT", "7707")
    return os.environ.get("MEILI_URL", f"http://localhost:{port}")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(_args: argparse.Namespace) -> int:
    root = _repo_root()
    env_path = root / ".env"
    if env_path.exists():
        print(f".env already exists — left untouched ({env_path}).")
        print("Secrets are never regenerated over a live stack.")
    else:
        env_path.write_text(
            f"POSTGRES_PASSWORD={secrets.token_hex(32)}\n"
            f"MEILI_MASTER_KEY={secrets.token_hex(32)}\n"
        )
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print(f"Wrote {env_path} with fresh random secrets (gitignored, mode 0600).")
    print("Next: make dev   # boots postgres:5437 + meilisearch:7707, applies schema, loads sample")
    print("Then: proptoit index")
    return 0


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def cmd_index(args: argparse.Namespace) -> int:
    root = _repo_root()
    _load_env(root)
    env = os.environ.copy()
    env["DATABASE_URL"] = args.database_url or _sample_dsn()
    env.setdefault("MEILI_URL", _sample_meili_url())

    script = root / "collectors" / "index_to_meili.py"
    argv = [sys.executable, str(script)]
    if args.county:
        argv += ["--county", args.county]
    if args.delete_first:
        argv.append("--delete-first")

    print(f"Indexing properties into Meilisearch ({env['MEILI_URL']}) ...")
    return subprocess.run(argv, env=env).returncode


# ---------------------------------------------------------------------------
# serve-mcp
# ---------------------------------------------------------------------------

def cmd_serve_mcp(_args: argparse.Namespace) -> int:
    root = _repo_root()
    # MCP clients pass a minimal env; make sure the server can reach the DB
    # before importing it. An operator-exported DATABASE_URL wins.
    _load_env(root)
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = _sample_dsn()

    sys.path.insert(0, str(root))
    import api.mcp_server  # noqa: E402  (registers all tools; sets up api/ paths)

    api.mcp_server.mcp.run()  # FastMCP default transport: stdio
    return 0


# ---------------------------------------------------------------------------
# triage (Pro, license-gated)
# ---------------------------------------------------------------------------

def cmd_triage(args: argparse.Namespace) -> int:
    from pro import PRO_LICENSE_MESSAGE, require_license

    # Call-time env read, gate on .valid — NEVER on truthiness of KeyInfo.
    info = require_license()
    if not info.valid:
        print(f"{PRO_LICENSE_MESSAGE} ({info.reason})", file=sys.stderr)
        return 2

    if not Path(args.csv).is_file():
        print(f"CSV not found: {args.csv}", file=sys.stderr)
        return 1

    _load_env(_repo_root())

    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        stream=sys.stderr,  # keep stdout for the result summary
    )
    from pro.triage.load_skip_trace_bulk import run

    result = run(
        args.csv,
        dry_run=not args.commit,
        limit=args.limit,
        dsn=args.database_url or os.environ.get("DATABASE_URL") or _sample_dsn(),
    )
    print(f"Matched:     {result.get('matched', 0)}")
    print(f"Unmatched:   {result.get('unmatched', 0)}")
    if result.get("dry_run"):
        print("Dry run — no DB writes (pass --commit to write).")
    else:
        print(f"Skip traces inserted: {result.get('skip_trace_inserted', 0)}")
        print(f"Properties updated:  {result.get('properties_updated', 0)}")
    return 0


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proptoit",
        description="PropToIt — Chattanooga off-market property intelligence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="ensure .env exists with random secrets (never overwrites)")

    p_index = sub.add_parser("index", help="reindex properties into Meilisearch")
    p_index.add_argument("--county", help="restrict to one county_id (default: all)")
    p_index.add_argument("--delete-first", action="store_true", help="drop the index before reindexing")
    p_index.add_argument("--database-url", help="override the target DSN (default: sample stack from .env)")

    sub.add_parser("serve-mcp", help="run the MCP server over stdio (Claude Desktop entry point)")

    p_triage = sub.add_parser("triage", help="[Pro] load an assessor bulk CSV into skip-trace results (license-gated)")
    p_triage.add_argument("csv", help="path to the assessor bulk CSV")
    p_triage.add_argument("--commit", action="store_true", help="write to the DB (default: dry run, no writes)")
    p_triage.add_argument("--limit", type=int, default=0, help="only process the first N rows")
    p_triage.add_argument("--database-url", help="override the target DSN (default: sample stack from .env)")

    args = parser.parse_args(argv)
    handler = {
        "init": cmd_init,
        "index": cmd_index,
        "serve-mcp": cmd_serve_mcp,
        "triage": cmd_triage,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
