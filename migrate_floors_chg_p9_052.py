#!/usr/bin/env python3
"""CHG-P9-052 (Sprint 2.5b): recalibrate task_classes.default_quality_floor onto
the ACCEPT-RATE scale for the pattern-heavy classes that Argos was no-routing.

WHY: route_select compares predicted_success -- which, once a route has >= MIN_OBS
real labels, is an ACCEPT-RATE from dispatches -- against the task-class floor,
which was seeded on a QUALITY-SCORE scale. devops observed accept 0.873 vs a 0.90
quality floor -> blocked, despite 87% of devops work succeeding. This script moves
the floors to the accept-rate scale so the (now dual) gate's accept predicate is
comparing like with like. The dual-gate code (CHG-P9-052/2) does the rest: the
benchmark-quality screen (strict mode) catches completed-but-sloppy.

This is a SCRIPT, not a blind UPDATE:
  * DRY-RUN by default. Prints a BEFORE/AFTER table; writes nothing.
  * --apply writes a rollback sidecar (the exact pre-apply values) BEFORE updating,
    so --rollback restores precisely. Re-runnable: a second --apply is a no-op for
    rows already at their target.
  * FIXED targets are documented inline with their OLD values for belt-and-braces
    rollback even without the sidecar.
  * DATA-DRIVEN targets only LOWER a floor when the observed accept-rate (>= 5
    labels) shows the current floor is blocking; classes with < 5 labels are left
    UNCHANGED.
  * PROTECTED classes (architecture_design, security, code_algorithmic) are NEVER
    lowered -- premium-by-policy.

NOT part of deploy. Andy / Chat-Claude runs and verifies this on garage:
    python3 migrate_floors_chg_p9_052.py            # dry-run table
    python3 migrate_floors_chg_p9_052.py --apply     # write + sidecar
    python3 migrate_floors_chg_p9_052.py --rollback  # restore from sidecar
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3

DEFAULT_DB = "/home/andy/argos/argos.db"
ROLLBACK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "migrate_floors_chg_p9_052.rollback.json")
MIN_OBS = 5  # below this many labels we have no basis to move a floor

# FIXED targets: (class_id -> (old_for_rollback, new_value, note)). The OLD value is
# the seed in phase0_schema.populate_task_classes at the time of this change.
FIXED = {
    # observed accept 0.873; 0.80 sits below it with margin (CHG brief).
    "devops": (0.90, 0.80, "observed accept 0.873; floor below it with margin"),
    # already on/near the accept scale; keep. Confirmed against observed at runtime.
    "analysis": (0.70, 0.70, "already near accept-scale; keep ~0.70"),
}

# DATA-DRIVEN: lower the floor to just-below observed accept ONLY when data says the
# current floor blocks. Margin below observed so the class clears with headroom.
DATA_DRIVEN = ["code_boilerplate", "formatting", "documentation",
               "docs_explainer", "conversation"]
DATA_MARGIN = 0.05

# PROTECTED: never lowered. Premium-by-policy -- a wrong answer here is expensive.
PROTECTED = {
    "architecture_design": 0.95,
    "security": 0.95,
    "code_algorithmic": 0.90,
}


def _observed_accept(con, class_id):
    """(n_labels, accept_rate or None) for a class across all models in dispatches."""
    try:
        row = con.execute(
            "SELECT COUNT(*) n, AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) rate "
            "FROM dispatches WHERE task_class=? AND accepted IS NOT NULL",
            (class_id,)).fetchone()
    except sqlite3.Error:
        return 0, None
    if not row:
        return 0, None
    return int(row[0] or 0), (None if row[1] is None else float(row[1]))


def _current_floor(con, class_id):
    row = con.execute(
        "SELECT default_quality_floor FROM task_classes WHERE class_id=?",
        (class_id,)).fetchone()
    return None if not row else (None if row[0] is None else float(row[0]))


def build_plan(con):
    """Return ordered list of plan rows: dicts with class_id, current, target,
    action ('lower'|'keep'|'unchanged'|'protected'|'missing'|'insufficient-data'),
    observed (n, rate), note."""
    plan = []

    for class_id, (old_seed, target, note) in FIXED.items():
        cur = _current_floor(con, class_id)
        n, rate = _observed_accept(con, class_id)
        if cur is None:
            plan.append(dict(class_id=class_id, current=None, target=None,
                             action="missing", observed=(n, rate),
                             note=f"class absent from task_classes (seed_old={old_seed})"))
            continue
        action = "keep" if abs((target or cur) - cur) < 1e-9 else "lower" if target < cur else "raise"
        plan.append(dict(class_id=class_id, current=cur, target=target,
                         action=action, observed=(n, rate),
                         note=f"{note} (seed_old={old_seed})"))

    for class_id in DATA_DRIVEN:
        cur = _current_floor(con, class_id)
        if cur is None:
            plan.append(dict(class_id=class_id, current=None, target=None,
                             action="missing", observed=(0, None),
                             note="class absent from task_classes"))
            continue
        n, rate = _observed_accept(con, class_id)
        if n < MIN_OBS or rate is None:
            plan.append(dict(class_id=class_id, current=cur, target=cur,
                             action="insufficient-data", observed=(n, rate),
                             note=f"{n} labels (< {MIN_OBS}); leave unchanged"))
            continue
        candidate = round(rate - DATA_MARGIN, 2)
        if candidate < cur:  # only LOWER, and only when current floor would block
            plan.append(dict(class_id=class_id, current=cur, target=candidate,
                             action="lower", observed=(n, rate),
                             note=f"observed accept {rate:.3f}; floor -> observed-{DATA_MARGIN:.2f}"))
        else:
            plan.append(dict(class_id=class_id, current=cur, target=cur,
                             action="unchanged", observed=(n, rate),
                             note=f"observed accept {rate:.3f}; current floor not blocking"))

    for class_id, premium in PROTECTED.items():
        cur = _current_floor(con, class_id)
        n, rate = _observed_accept(con, class_id)
        plan.append(dict(class_id=class_id, current=cur, target=cur,
                         action="protected", observed=(n, rate),
                         note=f"deliberately high ({premium}); never lowered"))

    return plan


def print_table(plan, header):
    print(f"\n{header}")
    print(f"  {'class_id':22s} {'before':>7s} {'after':>7s} {'Δ':>6s} "
          f"{'labels':>6s} {'accept':>7s}  action / note")
    print("  " + "-" * 96)
    for p in plan:
        n, rate = p["observed"]
        cur = "--" if p["current"] is None else f"{p['current']:.2f}"
        tgt = "--" if p["target"] is None else f"{p['target']:.2f}"
        if p["current"] is not None and p["target"] is not None:
            delta = p["target"] - p["current"]
            dlt = f"{delta:+.2f}" if abs(delta) > 1e-9 else "  ."
        else:
            dlt = "--"
        acc = "  --" if rate is None else f"{rate:.3f}"
        print(f"  {p['class_id']:22s} {cur:>7s} {tgt:>7s} {dlt:>6s} "
              f"{n:>6d} {acc:>7s}  {p['action']}: {p['note']}")
    changes = [p for p in plan if p["action"] in ("lower", "raise", "keep")
               and p["current"] is not None and p["target"] is not None
               and abs(p["target"] - p["current"]) > 1e-9]
    print(f"\n  {len(changes)} floor(s) would change.")


def apply_plan(con, plan):
    # Capture exact pre-apply values for the rows we will touch -> sidecar.
    touched = [p for p in plan if p["current"] is not None and p["target"] is not None
               and abs(p["target"] - p["current"]) > 1e-9]
    if not touched:
        print("\nNothing to apply (all rows already at target).")
        return
    rollback = {p["class_id"]: p["current"] for p in touched}
    with open(ROLLBACK_PATH, "w", encoding="utf-8") as f:
        json.dump(rollback, f, indent=2, sort_keys=True)
    print(f"\nWrote rollback sidecar ({len(rollback)} rows): {ROLLBACK_PATH}")
    for p in touched:
        con.execute("UPDATE task_classes SET default_quality_floor=? WHERE class_id=?",
                    (p["target"], p["class_id"]))
    con.commit()
    print(f"Applied {len(touched)} floor update(s).")


def rollback_plan(con):
    if not os.path.exists(ROLLBACK_PATH):
        # Fall back to the hard-coded FIXED olds (data-driven olds are unknown
        # without a sidecar, so only the fixed ones can be restored this way).
        print(f"No sidecar at {ROLLBACK_PATH}; restoring FIXED olds only.")
        restore = {cid: old for cid, (old, _new, _n) in FIXED.items()}
    else:
        with open(ROLLBACK_PATH, "r", encoding="utf-8") as f:
            restore = json.load(f)
        print(f"Restoring {len(restore)} row(s) from sidecar.")
    for class_id, old in restore.items():
        con.execute("UPDATE task_classes SET default_quality_floor=? WHERE class_id=?",
                    (old, class_id))
    con.commit()
    print("Rollback complete:", {k: round(v, 2) for k, v in restore.items()})


def main():
    ap = argparse.ArgumentParser(description="CHG-P9-052 floor recalibration (reversible)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"sqlite path (default {DEFAULT_DB})")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the recalibrated floors")
    g.add_argument("--rollback", action="store_true", help="restore pre-apply floors")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        if args.rollback:
            rollback_plan(con)
            print_table(build_plan(con), "AFTER ROLLBACK:")
            return
        plan = build_plan(con)
        print_table(plan, "DRY-RUN (no changes written)" if not args.apply
                    else "PLAN (about to apply):")
        if args.apply:
            apply_plan(con, plan)
            print_table(build_plan(con), "AFTER APPLY:")
        else:
            print("\nDry-run only. Re-run with --apply to write, --rollback to revert.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
