"""Argos Phase 0 backfill: pull dispatches from UM780 cost_log into argos.db on garage.
Maps fields, leaves task_class NULL for now (Haiku classification = follow-up).
"""
import sqlite3
import urllib.request
import ssl
import json
import time
import sys

ARGOS_DB = "/home/andy/argos/argos.db"
COST_LOG_REMOTE = "/home/andy/orchestrator/cost-log.db"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def fetch_cost_log_via_ssh():
    """We're running on garage, cost_log is on UM780. Use scp-pulled snapshot path."""
    # Caller writes a snapshot to /tmp/cost_log_snapshot.db before running this script.
    snap = "/tmp/cost_log_snapshot.db"
    db = sqlite3.connect(snap)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT id, ts, tag, model, cost_usd, status, provider_mode, notes, raw_json
        FROM cost_log
        ORDER BY ts DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

def main():
    log("Fetching cost_log snapshot from /tmp/cost_log_snapshot.db ...")
    rows = fetch_cost_log_via_ssh()
    log(f"Got {len(rows)} cost_log rows")

    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row

    inserted = 0
    skipped = 0
    for r in rows:
        # dispatch_id: composite of source_id (cost_log.id) prefixed
        did = f"costlog-{r['id']}"
        # extract optional fields from raw_json if present
        try:
            raw = json.loads(r['raw_json']) if r['raw_json'] else {}
        except Exception:
            raw = {}

        # Map input/output tokens if present
        in_tok = raw.get("input_tokens") or raw.get("usage", {}).get("input_tokens")
        out_tok = raw.get("output_tokens") or raw.get("usage", {}).get("output_tokens")

        # Try to insert; idempotent (PK conflict ignored)
        try:
            db.execute("""
                INSERT INTO dispatches (
                    dispatch_id, ts, source, provider_mode, model_used,
                    actual_input_tokens, actual_output_tokens, actual_cost_usd, status,
                    rework_cycles
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                did, r['ts'], 'cost_log_backfill', r['provider_mode'], r['model'],
                in_tok, out_tok, r['cost_usd'], r['status']
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.commit()

    # Stats
    log(f"Backfill done: {inserted} inserted, {skipped} skipped (duplicates)")

    cur = db.execute("""
        SELECT
            COUNT(*) total,
            COUNT(DISTINCT model_used) distinct_models,
            COUNT(DISTINCT provider_mode) distinct_providers,
            COUNT(DISTINCT date(ts)) distinct_days,
            ROUND(SUM(actual_cost_usd), 2) total_cost_usd,
            MIN(ts) earliest,
            MAX(ts) latest
        FROM dispatches
    """).fetchone()
    log(f"")
    log(f"=== dispatches table summary ===")
    log(f"  total dispatches:    {cur['total']}")
    log(f"  distinct models:     {cur['distinct_models']}")
    log(f"  distinct providers:  {cur['distinct_providers']}")
    log(f"  distinct days:       {cur['distinct_days']}")
    log(f"  total cost USD:      ${cur['total_cost_usd']}")
    log(f"  earliest:            {cur['earliest']}")
    log(f"  latest:              {cur['latest']}")

    log(f"")
    log(f"=== by provider_mode ===")
    for row in db.execute("""
        SELECT provider_mode, COUNT(*) n, ROUND(SUM(actual_cost_usd), 4) total
        FROM dispatches
        GROUP BY provider_mode
        ORDER BY n DESC
    """):
        log(f"  {row['provider_mode'] or '(null)':20}  n={row['n']:4}  total=${row['total']}")

    log(f"")
    log(f"=== by model_used (top 10) ===")
    for row in db.execute("""
        SELECT model_used, COUNT(*) n, ROUND(SUM(actual_cost_usd), 4) total, ROUND(AVG(actual_cost_usd), 5) mean
        FROM dispatches
        GROUP BY model_used
        ORDER BY n DESC
        LIMIT 10
    """):
        log(f"  {row['model_used'] or '(null)':45}  n={row['n']:4}  total=${row['total']:8}  mean=${row['mean']}")

    log(f"")
    log(f"=== by status ===")
    for row in db.execute("""
        SELECT status, COUNT(*) n
        FROM dispatches
        GROUP BY status
        ORDER BY n DESC
    """):
        log(f"  {row['status']:30}  {row['n']}")

    db.close()
    log("")
    log("Phase 0 backfill complete.")

if __name__ == "__main__":
    main()
