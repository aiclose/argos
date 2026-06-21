#!/usr/bin/env python3
"""Offline smoke test for U3-001: graduated multi-factor quality_score.

Runnable: `python3 tests/smoke_u3.py`. Builds a temp sqlite db (no network, no
touching the real argos.db), exercises each status tier + each adjustment, and
verifies backfill is graduated, COALESCE-safe, and idempotent.
"""
import os, sys, sqlite3, tempfile

# import the module under test (repo root is parent of this tests/ dir)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import outcome_labeler as ol

PASS = True
def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False

def mfs(status, **kw):
    """multi_factor_score shorthand for a synthetic row."""
    row = {"status": status}
    row.update(kw)
    return ol.multi_factor_score(row, kw.pop("medians", None))

print("== U3-001 smoke: multi_factor_score tiers + adjustments ==")

# --- tier ordering: clean success > completed_no_checkpoint > failure ---
clean = ol.multi_factor_score({"status": "completed"})
caveat = ol.multi_factor_score({"status": "completed_no_checkpoint"})
soft = ol.multi_factor_score({"status": "escalated_verify"})
fail = ol.multi_factor_score({"status": "failed_error"})
check("clean success > completed_no_checkpoint", clean > caveat)
check("completed_no_checkpoint > soft(escalated_verify)", caveat > soft)
check("soft > hard failure", soft > fail)
check("clean success > failure", clean > fail)

# --- absent signals are a no-op: status-only row sits exactly at its tier base ---
check("status-only completed == base 0.85", clean == 0.85)
check("status-only completed_no_checkpoint == base 0.70", caveat == 0.70)
check("status-only failed_error == base 0.20", fail == 0.20)

# --- rejected_zdr: policy-blocked, NOT scored (None) ---
check("rejected_zdr -> None (not scored)",
      ol.multi_factor_score({"status": "rejected_zdr"}) is None)
acc_zdr, q_zdr = ol.status_to_label("rejected_zdr")
check("rejected_zdr -> accepted None", acc_zdr is None)
# --- NULL / unknown status -> None ---
check("NULL status -> None", ol.multi_factor_score({"status": None}) is None)
check("unknown status -> None", ol.multi_factor_score({"status": "weird"}) is None)

# --- empty-output success scored BELOW a normal success ---
empty = ol.multi_factor_score({"status": "completed", "actual_output_tokens": 0})
normal = ol.multi_factor_score({"status": "completed", "actual_output_tokens": 500})
check("empty-output success < normal success", empty < normal)
check("normal success (tokens>0) unchanged == base", normal == 0.85)
# empty-output penalty must NOT fire on a failure status
fail_zero = ol.multi_factor_score({"status": "failed_error", "actual_output_tokens": 0})
check("empty-output no-op on failure status", fail_zero == 0.20)

# --- latency outlier scored at/below a normal row (needs a computable median) ---
meds = {("class", "code_gen"): 1000.0}
lat_out = ol.multi_factor_score(
    {"status": "completed", "task_class": "code_gen", "latency_ms": 9000}, meds)
lat_norm = ol.multi_factor_score(
    {"status": "completed", "task_class": "code_gen", "latency_ms": 1100}, meds)
check("latency outlier <= normal-latency row", lat_out <= lat_norm)
check("latency outlier strictly penalised", lat_out < clean)
check("in-range latency == base (no penalty)", lat_norm == 0.85)
# latency adjustment is a no-op when median not computable / latency absent
check("latency no-op when no medians",
      ol.multi_factor_score({"status": "completed", "latency_ms": 9000}, None) == 0.85)
check("latency no-op when latency absent",
      ol.multi_factor_score({"status": "completed", "task_class": "code_gen"}, meds) == 0.85)

# --- rework: no-op at 0, penalises when >0 ---
check("rework_cycles=0 is no-op",
      ol.multi_factor_score({"status": "completed", "rework_cycles": 0}) == 0.85)
check("rework_cycles>0 penalises",
      ol.multi_factor_score({"status": "completed", "rework_cycles": 2}) < 0.85)

# --- clamp to [0,1] under stacked penalties ---
worst = ol.multi_factor_score(
    {"status": "failed_error", "actual_output_tokens": 0, "rework_cycles": 9})
check("score clamped >= 0.0", worst >= 0.0)

print("== U3-001 smoke: backfill on temp db (graduated, COALESCE-safe) ==")
fd, dbpath = tempfile.mkstemp(suffix=".db")
os.close(fd)
orig_db = ol.DB
try:
    ol.DB = dbpath
    con = sqlite3.connect(dbpath)
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, status TEXT, accepted BOOLEAN,
        quality_score REAL, actual_output_tokens INTEGER, latency_ms INTEGER,
        rework_cycles INTEGER DEFAULT 0, task_class TEXT, model_used TEXT)""")
    # mixed batch: every tier + adjustments + a pre-scored row + a policy-blocked row
    seed = [
        ("d1", "completed",               None, None, 500,  None, 0, "code_gen", "m1"),
        ("d2", "ok",                       None, None, None, None, 0, "code_gen", "m1"),
        ("d3", "completed_no_checkpoint",  None, None, None, None, 0, "code_gen", "m1"),
        ("d4", "escalated_verify",         None, None, None, None, 0, "qa",       "m2"),
        ("d5", "failed_error",             None, None, None, None, 0, "qa",       "m2"),
        ("d6", "completed",                None, None, 0,    None, 0, "code_gen", "m1"),  # empty output
        ("d7", "rejected_zdr",             None, None, None, None, 0, "code_gen", "m1"),  # blocked
        ("d8", "completed",                1,    0.42, 500,  None, 0, "code_gen", "m1"),  # pre-scored
    ]
    con.executemany("INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?)", seed)
    con.commit(); con.close()

    ol.backfill()

    con = sqlite3.connect(dbpath)
    g = dict(con.execute("SELECT dispatch_id, quality_score FROM dispatches"))
    a = dict(con.execute("SELECT dispatch_id, accepted FROM dispatches"))

    check("d7 rejected_zdr: quality_score stays NULL", g["d7"] is None)
    check("d7 rejected_zdr: accepted stays NULL", a["d7"] is None)
    check("d8 pre-scored NOT clobbered (COALESCE) == 0.42", g["d8"] == 0.42)
    check("d6 empty-output success < d1 normal success", g["d6"] < g["d1"])
    check("d1 clean success > d3 completed_no_checkpoint", g["d1"] > g["d3"])
    check("d3 completed_no_checkpoint > d5 failure", g["d3"] > g["d5"])

    distinct = sorted({v for v in g.values() if v is not None})
    check(f"distribution is NOT 2 spikes (distinct={distinct})", len(distinct) > 2)

    # idempotent: a second backfill changes nothing
    ol.backfill()
    con2 = sqlite3.connect(dbpath)
    g2 = dict(con2.execute("SELECT dispatch_id, quality_score FROM dispatches"))
    check("idempotent: scores unchanged on re-run", g == g2)
    con2.close(); con.close()
finally:
    ol.DB = orig_db
    os.remove(dbpath)

print()
print("SMOKE U3: ALL PASS" if PASS else "SMOKE U3: FAIL")
sys.exit(0 if PASS else 1)
