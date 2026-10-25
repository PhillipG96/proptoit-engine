# PropToIt Engine

**Self-hosted deal analysis engine for real-estate investors — bring your data, plug in your AI.**

PropToIt runs on your machine, indexes public-records property data, and exposes it to
your AI assistant over [MCP](https://modelcontextprotocol.io) — 20 tools (19 read-only
plus `save_agent_memory`, which writes agent notes to your local database). No cloud,
no phone-home. Pro features (deal scoring, ARV, bulk triage) unlock with a
one-time license; the free tier needs no key.

## Quickstart (10 minutes)

Requires Docker and Python 3.10+.

```bash
git clone https://github.com/PhillipG96/proptoit-engine
cd proptoit-engine
pip install -e .        # installs the proptoit CLI + MCP server (from the repo root)
proptoit init           # writes .env with fresh random secrets (never overwrites an existing one)
make dev                # boots postgres :5437 + meilisearch :7707, applies schema, loads the 250-parcel Hamilton County sample
proptoit index          # indexes the sample into meilisearch
```

Then point Claude Desktop (or any MCP client) at it — free tier, no key:

```json
{
  "mcpServers": {
    "proptoit": {
      "command": "proptoit",
      "args": ["serve-mcp"],
      "env": {
        "PROPTOIT_LICENSE_KEY": "PTOIT1.…"
      }
    }
  }
}
```

Omit the `env` block entirely for the free tier. With Pro, ask:
*"Score my Hamilton County lead list — tax-delinquent single-family under $250k."*
A real recorded session (unkeyed denial → offline license → scored leads) is in
[docs/demo/demo.gif](docs/demo/demo.gif).

## Free vs Pro

19 of the 20 MCP tools are read-only — the exception is `save_agent_memory`,
which writes agent notes to your local database. Free tools work with no license; the four Pro
tools stay discoverable but return a `license_required` error until
`PROPTOIT_LICENSE_KEY` is set (verification is fully offline, Ed25519 —
no network calls, ever).

| | Tools |
|---|---|
| **Free (16)** | `search_properties`, `get_property_detail`, `list_foreclosures`, `get_distress_signals`, `list_fsbo`, `assemble_context`, `save_agent_memory`, `compare_properties`, `list_distressed_with_coords`, `get_system_stats`, `search_by_category`, `get_foreclosure_stats`, `meilisearch_search`, `get_market_context`, `get_comps`, `list_counties` |
| **Pro (4)** | `score_leads`, `get_deal_analysis`, `get_top_deals`, `get_arv` |
| **Pro CLI** | `proptoit triage` — bulk-load an assessor CSV into skip-trace results (fail-closed, dry-run by default) |

Pro is a one-time license: [proptoit.com/pricing](https://proptoit.com/pricing).
To self-issue a dev license: `python3 scripts/license_keygen.py keygen` writes an
Ed25519 keypair (private key to `~/.hermes/secrets/proptoit-engine/`, mode 0600,
never committed), paste the public key into `pro/license.py`, then
`python3 scripts/license_keygen.py sign --tier personal --email you@example.com --exp 2027-12-31`
prints a license key. Set it via `export PROPTOIT_LICENSE_KEY=…` — rotatable
without restarting the MCP server.

![Pro gate demo: unlicensed denial, offline license signing, scored lead list via MCP](docs/demo/demo.gif)

## Bring your own county

`hamilton` (TN) ships as the reference county adapter:
[`proptoit/adapters/hamilton/hamilton.yaml`](proptoit/adapters/hamilton/hamilton.yaml),
loaded through [`proptoit/county_config.py`](proptoit/county_config.py). Adding a
county means adding an adapter package with its own YAML (parcel table, assessor
endpoints, field maps) — no collector code changes. The 250-parcel sample dataset
is PII-safe by construction; see [sample/README.md](sample/README.md).

---

Source of truth for the tool split: [`api/mcp_server.py`](api/mcp_server.py)
(free tools) and the `_pro_license_gate` wiring (Pro tools). License contract:
[`pro/license.py`](pro/license.py).

## License

MIT — see [LICENSE](LICENSE). The four Pro tools are gated by a one-time license key at runtime; the code itself is MIT.
