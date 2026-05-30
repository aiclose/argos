"""Argos outcome labeler.

Derives the `accepted` label (and a coarse quality_score) from the `status` that
dispatches already capture, so the quality predictor has real data instead of 2
hand-labelled rows. This is the honest first step: use the success signal that
already exists before building elaborate new capture.

Status -> outcome mapping (conservative):
  completed, completed_no_checkpoint, ok   -> accepted=1 (success)
  errored, failed_error, failed_cost_cap   -> accepted=0 (failure)
  NULL / unknown                           -> leave NULL (no signal)

quality_score: we only have binary success right now, so set a coarse proxy:
  success -> 0.8, failure -> 0.2, leave finer scoring to real rubric/judge later.
This proxy is deliberately crude and flagged as such; it gives the predictor a
gradient without pretending to rubric-level precision.

Usable as: backfill (label all existing) and a function dispatch_tail can call
per new row.
"""
import sqlite3, datetime

DB = "/home/andy/argos/argos.db"

SUCCESS_STATUSES = {"completed", "completed_no_checkpoint", "ok"}
FAILURE_STATUSES = {"errored", "failed_error", "failed_cost_cap"}

def status_to_label(status):
    """Return (accepted, quality_proxy) or (None, None) if no signal."""
    if status in SUCCESS_STATUSES:
        return 1, 0.8
    if status in FAILURE_STATUSES:
        return 0, 0.2
    # status LIKE 'failed%' catch-all
    if status and status.startswith("failed"):
        return 0, 0.2
    return None, None

def backfill(dry_run=False):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT dispatch_id, status, accepted FROM dispatches").fetchall()
    to_label = 0
    for did, status, accepted in rows:
        if accepted is not None:
            continue  # already labelled (don't overwrite real labels)
        acc, q = status_to_label(status)
        if acc is None:
            continue
        to_label += 1
        if not dry_run:
            # only set quality_score if currently NULL (don't clobber real scores)
            con.execute(
                "UPDATE dispatches SET accepted=?, "
                "quality_score=COALESCE(quality_score, ?) WHERE dispatch_id=?",
                (acc, q, did))
    if not dry_run:
        con.commit()
    # report
    total = con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    labeled = con.execute("SELECT COUNT(*) FROM dispatches WHERE accepted IS NOT NULL").fetchone()[0]
    print(f"{'DRY-RUN: would label' if dry_run else 'labelled'} {to_label} dispatches")
    print(f"dispatches now labelled: {labeled}/{total}")
    print("--- accept rate by task_class (top, now that labels exist) ---")
    for r in con.execute(
        "SELECT task_class, COUNT(*) n, ROUND(AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END),2) rate "
        "FROM dispatches WHERE accepted IS NOT NULL GROUP BY task_class ORDER BY n DESC LIMIT 8"):
        print("  ", r)
    print("--- accept rate by model_used (the fit signal we wanted) ---")
    for r in con.execute(
        "SELECT model_used, COUNT(*) n, ROUND(AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END),2) rate "
        "FROM dispatches WHERE accepted IS NOT NULL GROUP BY model_used ORDER BY n DESC LIMIT 8"):
        print("  ", r)
    con.close()

if __name__ == "__main__":
    import sys
    backfill(dry_run=("--dry" in sys.argv))
