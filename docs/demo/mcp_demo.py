#!/usr/bin/env python3
"""Demo driver: speak real MCP stdio JSON-RPC to `proptoit serve-mcp`.

usage: mcp_demo.py {unlicensed|licensed}

This is exactly what an MCP client (Claude Desktop, Claude Code) does:
initialize handshake, then tools/call score_leads. Output is the real
response from the live sample stack (localhost:5437).
"""
import json
import os
import subprocess
import sys

if sys.argv[-1] == "unlicensed":
    os.environ.pop("PROPTOIT_LICENSE_KEY", None)

proc = subprocess.Popen(
    ["proptoit", "serve-mcp"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True,
)


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def read_id(i):
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == i:
            return msg
    return {}


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "demo", "version": "0.1.0"}}})
read_id(1)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "score_leads", "arguments": {
          "max_total_investment": 250000,
          "min_equity_pct": 20,
          "preferred_signals": ["tax_delinquent"],
      }}})
resp = read_id(2)
content = json.loads(resp["result"]["content"][0]["text"])
if "error" in content:
    print(json.dumps(content, indent=2))
else:
    deals = content["deals"][:3]
    print(f'count: {content["count"]} (showing top 3)')
    for d in deals:
        flags = ",".join(k.replace("Flag", "") for k in ("preForeclosureFlag", "taxDelinquentFlag",
                                                         "codeViolationFlag", "probateFlag", "fsboFlag") if d.get(k))
        print(f'  {d["address"]}  {d["city"]}, {d["state"]} {d["postalCode"]}'
              f'  assessed ${d["assessedValue"]:,.0f}  dealScore {d["dealScore"]}'
              f'  [{flags}]')
proc.terminate()
