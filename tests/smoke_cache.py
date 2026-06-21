#!/usr/bin/env python3
"""
Offline smoke test for CACHE-001 benchmark-score caching in weekly_eval_sprt.py.

Runs fully offline against a temp sqlite db. Monkeypatches the module's run_trial
with a fake that COUNTS its calls and hits NO network, so every assertion is about
whether the cache avoided a re-judge -- not about real model output.

Run:  python3 tests/smoke_cache.py
"""
import os, sys, sqlite3, tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import weekly_eval_sprt as W  # noqa: E402

# --- fake run_trial: counts calls, no network ---------------------------------
CALLS = {"n": 0}
FAKE_RESULT = (1, 0.85, 123)  # (success:int, score:float, latency_ms:int)

def fake_run_trial(model, task):
    CALLS["n"] += 1
    return FAKE_RESULT

W.run_trial = fake_run_trial  # cached_run_trial reads the module global run_trial

RESULTS = []
def check(name, cond):
    RESULTS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

def shape_ok(r3):
    """r3 = (success, score, latency); verify run_trial's tuple shape/types."""
    return (isinstance(r3[0], int) and isinstance(r3[1], float)
            and isinstance(r3[2], int))

def main():
    orig_judge = W.JUDGE_MODEL
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = tmp.name
    conn = sqlite3.connect(db)
    W.ensure_bench_cache(conn)
    task = {"id": "task-1", "prompt": "p"}

    # 1. MISS: first call for (model,task,judge) -> run_trial called once, cached.
    CALLS["n"] = 0
    r = W.cached_run_trial(conn, "modelA", task)
    check("MISS: run_trial called once", CALLS["n"] == 1)
    check("MISS: cache_state == 'miss'", r[3] == "miss")
    check("MISS: tuple value == fake result", (r[0], r[1], r[2]) == FAKE_RESULT)
    check("MISS: tuple shape (int,float,int)", shape_ok(r[:3]))

    # 2. HIT: identical call -> run_trial NOT called again, value from cache.
    CALLS["n"] = 0
    r2 = W.cached_run_trial(conn, "modelA", task)
    check("HIT: run_trial NOT called again", CALLS["n"] == 0)
    check("HIT: cache_state == 'hit'", r2[3] == "hit")
    check("HIT: cached tuple matches", (r2[0], r2[1], r2[2]) == FAKE_RESULT)
    check("HIT: tuple shape (int,float,int)", shape_ok(r2[:3]))

    # 3. STALE: force scored_at older than TTL -> re-runs and refreshes scored_at.
    old = (datetime.now(timezone.utc)
           - timedelta(days=W.BENCH_CACHE_TTL_DAYS + 5)).isoformat()
    conn.execute("UPDATE bench_cache SET scored_at=? WHERE model_id=? AND task_id=? "
                 "AND judge_model=?", (old, "modelA", "task-1", W.JUDGE_MODEL))
    conn.commit()
    CALLS["n"] = 0
    r3 = W.cached_run_trial(conn, "modelA", task)
    check("STALE: run_trial called (re-run)", CALLS["n"] == 1)
    check("STALE: cache_state == 'stale'", r3[3] == "stale")
    sa = conn.execute("SELECT scored_at FROM bench_cache WHERE model_id='modelA' "
                      "AND task_id='task-1' AND judge_model=?",
                      (W.JUDGE_MODEL,)).fetchone()[0]
    check("STALE: scored_at refreshed to fresh", W._age_days(sa) < W.BENCH_CACHE_TTL_DAYS)

    # 4. DIFFERENT judge_model -> separate cache row, old score bypassed (MISS).
    CALLS["n"] = 0
    W.JUDGE_MODEL = "different/judge-model"
    r4 = W.cached_run_trial(conn, "modelA", task)
    check("JUDGE-CHANGE: bypasses old cache (run_trial called)", CALLS["n"] == 1)
    check("JUDGE-CHANGE: cache_state == 'miss'", r4[3] == "miss")
    rows = conn.execute("SELECT COUNT(*) FROM bench_cache WHERE model_id='modelA' "
                        "AND task_id='task-1'").fetchone()[0]
    check("JUDGE-CHANGE: separate row per judge (2 rows)", rows == 2)
    W.JUDGE_MODEL = orig_judge  # restore: original (model,task,judge) row is fresh again

    # 5. force: a fresh hit is ignored and re-run.
    CALLS["n"] = 0
    pre = W.cached_run_trial(conn, "modelA", task)  # confirm it's a fresh hit first
    check("FORCE-precondition: would be a hit", pre[3] == "hit" and CALLS["n"] == 0)
    CALLS["n"] = 0
    r5 = W.cached_run_trial(conn, "modelA", task, force=True)
    check("FORCE: re-runs despite fresh hit", CALLS["n"] == 1)
    check("FORCE: cache_state != 'hit'", r5[3] != "hit")

    # 6. Cache I/O failure -> fall back to run_trial, valid result, no raise.
    bad = sqlite3.connect(db)
    bad.close()  # closed conn: every execute() raises -> must be swallowed
    CALLS["n"] = 0
    raised = False
    r6 = None
    try:
        r6 = W.cached_run_trial(bad, "modelA", task)
    except Exception:
        raised = True
    check("CACHE-FAIL: did NOT raise", not raised)
    check("CACHE-FAIL: fell back to run_trial", CALLS["n"] == 1)
    check("CACHE-FAIL: returned valid tuple", r6 is not None
          and (r6[0], r6[1], r6[2]) == FAKE_RESULT and shape_ok(r6[:3]))

    conn.close()
    try:
        os.unlink(db)
    except OSError:
        pass

    print()
    if all(RESULTS):
        print("SMOKE CACHE: ALL PASS")
        return 0
    print(f"SMOKE CACHE: FAIL ({RESULTS.count(False)}/{len(RESULTS)} checks failed)")
    return 1

if __name__ == "__main__":
    sys.exit(main())
