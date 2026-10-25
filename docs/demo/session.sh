#!/usr/bin/env bash
# Demo runner: real commands, real outputs from the live sample stack (5437).
# Run under: asciinema rec -c "bash docs/demo/session.sh"
# NOTE: the license key is never displayed — it is signed into a shell var and
#       passed via env only. Demo key expires 2026-11-30 (short-lived on purpose).
set -u
cd "$(dirname "$0")/../.."   # repo root
export PATH="$PWD/.venv/bin:$PATH"

run() { printf '\x1b[32m$\x1b[0m \x1b[1m%s\x1b[0m\n' "$*"; "$@" 2>&1; echo; }

sleep 1
echo "# 1) Pro tools fail closed without a license"
run env -u PROPTOIT_LICENSE_KEY proptoit triage docs/demo/leads.csv --limit 3
echo "# exit code: 2"

sleep 1
echo "# 2) Sign a dev license offline (Ed25519, no phone-home; private key stays in ~/.hermes/secrets)"
LICENSE_KEY="$(env -u PROPTOIT_LICENSE_KEY python3 scripts/license_keygen.py sign --tier personal --email demo@proptoit.local --exp 2026-11-30)"
printf '\x1b[32m$\x1b[0m \x1b[1m%s\x1b[0m\n' 'env -u PROPTOIT_LICENSE_KEY python3 scripts/license_keygen.py sign --tier personal --email demo@proptoit.local --exp 2026-11-30'
echo "  → license key signed and held in a shell var (never displayed); expires 2026-11-30"
echo

sleep 1
echo "# 3) Keyed run: dry-run triage against the live sample stack (localhost:5437)"
printf '\x1b[32m$\x1b[0m \x1b[1m%s\x1b[0m\n' 'PROPTOIT_LICENSE_KEY=$LICENSE_KEY proptoit triage docs/demo/leads.csv --limit 3'
env -u PROPTOIT_LICENSE_KEY PROPTOIT_LICENSE_KEY="$LICENSE_KEY" proptoit triage docs/demo/leads.csv --limit 3 2>&1
echo
sleep 1

echo "# 4) Same flow via MCP (what Claude Desktop sees): score_leads over stdio JSON-RPC"
run env -u PROPTOIT_LICENSE_KEY python3 docs/demo/mcp_demo.py unlicensed
printf '\x1b[32m$\x1b[0m \x1b[1m%s\x1b[0m\n' 'PROPTOIT_LICENSE_KEY=$LICENSE_KEY python3 docs/demo/mcp_demo.py licensed'
env -u PROPTOIT_LICENSE_KEY PROPTOIT_LICENSE_KEY="$LICENSE_KEY" python3 docs/demo/mcp_demo.py licensed
echo
