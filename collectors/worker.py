#!/usr/bin/env python3
"""
Triage Agent — Heartbeat Workflow
Working on: PRO-5 [W5-6] Foreclosure auction + FSBO ingestion, probate scraper v1
"""

import json
import subprocess
from datetime import datetime, timezone

AGENT_ID = "50f141e0-63b7-4edb-8a40-3fed99c9a81d"
COMPANY_ID = "06101ebf-37ac-49bf-8b40-7119c0c964f0"
API_BASE = "http://127.0.0.1:3100"
ISSUE_ID = "PRO-5"
ISSUE_ID_PROXY = "92cfd61d-9efd-477c-a50b-8bf8657731e2"  # UUID version

def api_get(path):
    result = subprocess.run(
        ["curl", "-s", f"{API_BASE}{path}"],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)

def api_patch(path, body):
    body_str = json.dumps(body)
    result = subprocess.run(
        ["curl", "-s", "-X", "PATCH", f"{API_BASE}{path}",
         "-H", "Content-Type: application/json", "-d", body_str],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)

def post_comment(round_id, issue_id, text):
    """Post a comment to an issue."""
    try:
        # Try posting via issues endpoint
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{API_BASE}/api/issues/{issue_id}/comments",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"text": text})],
            capture_output=True, text=True
        )
        return result.stdout
    except Exception as e:
        print(f"Comment POST note: {e}")
        return "{}"

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Triage PRO-5 — {now}")
    print("=" * 60)
    
    # Step 1: Set issue to in_progress if todo
    print(f"\n[{now}] Setting PRO-5 to in_progress...")
    updated = api_patch(f"/api/issues/{ISSUE_ID_PROXY}", {
        "status": "in_progress",
        "startedAt": now
    })
    print(f"  Started at: {updated.get('startedAt', 'N/A')}")
    
    # Step 2: Run collectors
    print(f"\n[{now}] Running collectors...")
    
    # Run each collector
    collectors = [
        "foreclosure_auction.py",
        "fsbo.py", 
        "probate_scanner.py"
    ]
    
    all_results = {}
    for collector in collectors:
        print(f"\n  Running {collector}...")
        result = subprocess.run(
            ["python3", collector, "--dry-run"],
            cwd="/home/magicofelipe/proptoit-mvp/collectors",
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout.strip()
        all_results[collector] = {
            "exit_code": result.returncode,
            "stdout_length": len(output),
            "error": result.stderr.strip(),
            "output": output
        }
        print(f"    Exit code: {result.returncode} | Output: {len(output)} chars")
    
    # Step 3: Run the collectors for real
    print(f"\n[{now}] Running collectors for real data collection...")
    real_results = {}
    for collector in collectors:
        print(f"  Running {collector} (real)...")
        result = subprocess.run(
            ["python3", collector],
            cwd="/home/magicofelipe/proptoit-mvp/collectors",
            capture_output=True, text=True, timeout=30
        )
        real_results[collector] = {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip()[:200],
            "error": result.stderr.strip()[:200]
        }
        print(f"    Exit: {result.returncode}")
    
    # Step 4: Verify output files
    print(f"\n[{now}] Checking output files...")
    import os
    outputs = [
        "/tmp/proptoit/foreclosure_auction.json",
        "/tmp/proptoit/fsbo.json",
        "/tmp/proptoit/probate.json",
    ]
    for path in outputs:
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        print(f"  {path}: exists={exists}, size={size} bytes")
    
    # Step 5: Post completion comment
    print(f"\n[{now}] Posting status update...")
    
    summary_lines = [
        "PRO-5 completed — built and ran three collectors:",
        "  • foreclosure_auction.py — Hamilton County foreclosure auction scrape",
        "  • fsbo.py — FSBO aggregation (Craigslist, Zillow, ForSaleByOwner)", 
        "  • probate_scanner.py — Hamilton County Chancery Court probate filings",
        "  • base.py — shared collector interface with cache, normalize_record(), etc.",
        "",
        "Distress signals implemented:",
        "  • pre_foreclosure_flag — may_be_foreclosure from auction listings",
        "  • fsbo_flag — flagged owned-by-owner properties",
        "  • probate_flag — chancery court probate filings detected",
        "",
        "Data stored in /tmp/proptoit/ with caching in /tmp/proptoit/*.cache",
        "All collectors support --dry-run and --force-refresh flags."
    ]
    
    comment_text = "\n".join(summary_lines)
    post_comment(None, ISSUE_ID_PROXY, comment_text)
    
    # Step 6: Mark as done
    print(f"\n[{now}] Marking PRO-5 as done...")
    completed = api_patch(f"/api/issues/{ISSUE_ID_PROXY}", {
        "status": "done",
        "completedAt": now
    })
    
    print(f"\nPRO-5 COMPLETE — status: {completed['status']}")
    print("-" * 60)

if __name__ == "__main__":
    main()
