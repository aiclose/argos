#!/usr/bin/env python3
"""Offline smoke for CHG-P9-050 Sprint 3: classify-and-route + outcome ingest.

Runnable: `python3 tests/smoke_sprint3.py`. Builds a temp sqlite db (no network,
never touches the real argos.db), points route_select at it, and drives the
fastapi-free core in sprint3_endpoints.py in-process (the same code the FastAPI
/classify-and-route and /outcome handlers call).

The live Haiku classifier (dispatch_tail.classify_one) needs LiteLLM + a key, which
this box does not have, so test 1 injects a deterministic keyword classifier. Test 0
proves the classify_one / classify_batch refactor itself (they share _haiku_classify)
without a network call by stubbing that single seam.

Covers the brief:
  0. classify_one + classify_batch both ride the shared _haiku_classify seam.
  1. /classify-and-route on 3 task_texts (devops/analysis/code) -> sane task_class
     and a route (route_id non-null, or an explicit no-route with reason).
  2. /outcome new tag -> row inserted, accepted derived from status; re-POST SAME tag
     -> NO duplicate (count stays 1), fields updated; judged-row guard holds.
  3. Read path: >=5 outcomes for one route+class -> select_route Tier-1 reflects the
     observed accept rate (predicted_success == rate, no warm-start prior_note).
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_select          # noqa: E402
import dispatch_tail         # noqa: E402
import sprint3_endpoints     # noqa: E402
from route_select import RouteTask  # noqa: E402

PASS = True


def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


def build_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE routes (
        route_id TEXT PRIMARY KEY, backend TEXT, tool TEXT, access_path TEXT,
        model_id TEXT, cost_mode TEXT, enabled INTEGER DEFAULT 1,
        healthcheck_type TEXT, last_health TEXT, quota_bucket TEXT)""")
    con.executemany(
        "INSERT INTO routes (route_id, backend, tool, access_path, model_id, "
        "cost_mode, enabled, healthcheck_type, last_health) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # one healthy spine route (api-chat probed ok) - so the Tier-1 read-path
            # test deterministically selects THIS model (claude-haiku).
            ("spine:litellm:claude-haiku", "spine", "litellm", "litellm",
             "claude-haiku", "per_token", 1, "api-chat", "ok"),
            # one healthy forge route (cli-smoke ok) - the not-spine backend anchor.
            ("forge:opencode:glm", "forge", "opencode", "openrouter",
             "z-ai/glm-4.6", "per_token", 1, "cli-smoke", "ok"),
        ],
    )
    # Taxonomy: id + description (classify prompt) + low floor (any route clears).
    con.execute("CREATE TABLE task_classes (class_id TEXT PRIMARY KEY, "
                "description TEXT, default_quality_floor REAL)")
    con.executemany(
        "INSERT INTO task_classes (class_id, description, default_quality_floor) VALUES (?,?,?)",
        [("devops", "deployment / infra / CI", 0.5),
         ("analysis", "investigate / summarise / report", 0.5),
         ("code_implementation", "write / implement code", 0.5),
         ("conversation", "fallback chit-chat", 0.5)],
    )
    # tables the selection/cost path reads; empty is fine (cost handles missing prices).
    con.execute("CREATE TABLE model_prices (model_id TEXT PRIMARY KEY, "
                "input_per_1m_usd REAL, output_per_1m_usd REAL, request_overhead_usd REAL)")
    # FULL dispatches schema (matches phase0_schema), so UPSERT + read path are real.
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, ts TIMESTAMP, source TEXT, provider_mode TEXT,
        model_used TEXT, task_class TEXT, domain TEXT, complexity_score REAL,
        reasoning_depth REAL, ambiguity_score REAL, error_sensitivity TEXT,
        estimated_input_tokens INTEGER, estimated_output_tokens INTEGER,
        actual_input_tokens INTEGER, actual_output_tokens INTEGER, actual_cost_usd REAL,
        latency_ms INTEGER, status TEXT, rework_cycles INTEGER DEFAULT 0,
        quality_score REAL, accepted BOOLEAN)""")
    con.commit()
    con.close()


def fake_classifier(task_text, classes_str, litellm_key):
    """Deterministic stand-in for dispatch_tail.classify_one (no LiteLLM on this box)."""
    t = (task_text or "").lower()
    if any(w in t for w in ("kubernetes", "deploy", "docker", "ci/cd", "pipeline")):
        return "devops"
    if any(w in t for w in ("analyse", "analyze", "summar", "report", "investigate")):
        return "analysis"
    if any(w in t for w in ("function", "implement", "code", "refactor", "endpoint")):
        return "code_implementation"
    return "conversation"


def row_count(path, tag):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM dispatches WHERE dispatch_id=?", (tag,)).fetchone()[0]
    finally:
        con.close()


def fetch_row(path, tag):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM dispatches WHERE dispatch_id=?", (tag,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


# ---------------------------------------------------------------------------
def test0_refactor_shared_seam():
    print("\n== TEST 0: classify_one / classify_batch share _haiku_classify ==")
    orig = dispatch_tail._haiku_classify
    try:
        # classify_batch parses a JSON array off the shared seam.
        dispatch_tail._haiku_classify = lambda prompt, system, key, max_tokens=500: (
            '["devops", "analysis"]', {"total_tokens": 1})
        res, usage = dispatch_tail.classify_batch(
            [{"tag": "a", "notes": "deploy"}, {"tag": "b", "notes": "report"}], "cls", "k")
        check("classify_batch returns parsed list", res == ["devops", "analysis"])

        # classify_one parses a single class_id off the SAME seam.
        dispatch_tail._haiku_classify = lambda prompt, system, key, max_tokens=500: (
            "code_implementation", {})
        one = dispatch_tail.classify_one("write a function", "cls", "k")
        check("classify_one returns single class_id", one == "code_implementation")

        # tolerant parse: fenced / array / stray-word answers still yield one id.
        dispatch_tail._haiku_classify = lambda *a, **k: ('["devops"]', {})
        check("classify_one tolerates array answer",
              dispatch_tail.classify_one("x", "c", "k") == "devops")
        dispatch_tail._haiku_classify = lambda *a, **k: ("analysis  (best guess)", {})
        check("classify_one strips stray words",
              dispatch_tail.classify_one("x", "c", "k") == "analysis")

        # error seam -> (None,None) for batch, None for one.
        dispatch_tail._haiku_classify = lambda *a, **k: (None, None)
        check("classify_batch -> (None,None) on seam error",
              dispatch_tail.classify_batch([{"tag": "a", "notes": "x"}], "c", "k") == (None, None))
        check("classify_one -> None on seam error",
              dispatch_tail.classify_one("x", "c", "k") is None)
    finally:
        dispatch_tail._haiku_classify = orig


def test1_classify_and_route(path):
    print("\n== TEST 1: /classify-and-route on 3 task_texts ==")
    samples = [
        ("S3-DEVOPS", "Set up a Kubernetes deployment pipeline for the staging cluster"),
        ("S3-ANALYSIS", "Investigate the latency regression and summarise root cause"),
        ("S3-CODE", "Implement the /outcome endpoint handler function"),
    ]
    expected = {"S3-DEVOPS": "devops", "S3-ANALYSIS": "analysis", "S3-CODE": "code_implementation"}
    for tag, text in samples:
        body, code = sprint3_endpoints.classify_and_route(
            task_text=text, tag=tag, db_path=path, classifier=fake_classifier)
        route = body.get("route_id")
        err = (body.get("error") or {}).get("code") if body.get("error") else None
        print(f"  [{tag}] class={body.get('task_class')!r} route={route!r} "
              f"backend={body.get('backend')} cleared={body.get('cleared_floor')} "
              f"http={code} err={err}")
        check(f"{tag}: http 200", code == 200)
        check(f"{tag}: classified {expected[tag]}", body.get("task_class") == expected[tag])
        # sane route: either a non-null route_id, or an explicit no-route reason.
        check(f"{tag}: route_id non-null OR explicit no-route reason",
              bool(route) or bool(err))

    # error path: empty task_text -> clean non-500 error, not a crash.
    body, code = sprint3_endpoints.classify_and_route(
        task_text="  ", tag="S3-EMPTY", db_path=path, classifier=fake_classifier)
    check("empty task_text -> 400 with error", code == 400 and body.get("error"))


def test2_outcome_upsert(path):
    print("\n== TEST 2: /outcome idempotent UPSERT keyed on tag ==")
    tag = "S3-OUTCOME-1"
    # New tag, completed -> accepted=1 (status_to_label source of truth).
    body, code = sprint3_endpoints.record_outcome(
        {"tag": tag, "task_class": "analysis", "model_id": "claude-haiku",
         "cost_usd": 0.0021, "status": "completed", "latency_ms": 1200}, db_path=path)
    print(f"  insert: action={body.get('action')} accepted={body.get('accepted')} http={code}")
    check("insert: http 200", code == 200)
    check("insert: row exists", row_count(path, tag) == 1)
    check("insert: accepted derived =1 from 'completed'", fetch_row(path, tag)["accepted"] == 1)

    # Re-POST SAME tag, failed -> NO duplicate, fields updated (accepted now 0).
    body, code = sprint3_endpoints.record_outcome(
        {"tag": tag, "status": "failed", "cost_usd": 0.0099}, db_path=path)
    r = fetch_row(path, tag)
    print(f"  re-post: action={body.get('action')} count={row_count(path, tag)} "
          f"accepted={r['accepted']} cost={r['actual_cost_usd']}")
    check("re-post: NO duplicate row (count stays 1)", row_count(path, tag) == 1)
    check("re-post: accepted updated to 0 from 'failed'", r["accepted"] == 0)
    check("re-post: cost updated", abs(r["actual_cost_usd"] - 0.0099) < 1e-9)

    # COALESCE-safe: a None field must NOT clobber an existing populated value.
    body, code = sprint3_endpoints.record_outcome(
        {"tag": tag, "status": "completed", "cost_usd": None}, db_path=path)
    r = fetch_row(path, tag)
    check("re-post: None cost did NOT null-clobber existing cost",
          abs(r["actual_cost_usd"] - 0.0099) < 1e-9)
    check("re-post: model_used preserved (None did not clobber)", r["model_used"] == "claude-haiku")

    # Judged-row guard: a row with a judge's quality_score is never re-accepted.
    jtag = "S3-JUDGED"
    con = sqlite3.connect(path)
    con.execute("INSERT INTO dispatches (dispatch_id, ts, task_class, model_used, "
                "status, accepted, quality_score) VALUES (?,?,?,?,?,?,?)",
                (jtag, "2026-06-22 00:00:00", "analysis", "claude-haiku", "completed", 1, 0.91))
    con.commit(); con.close()
    body, code = sprint3_endpoints.record_outcome(
        {"tag": jtag, "status": "failed", "cost_usd": 0.005}, db_path=path)
    r = fetch_row(path, jtag)
    print(f"  judged: action={body.get('action')} accepted={r['accepted']} "
          f"quality={r['quality_score']} cost={r['actual_cost_usd']}")
    check("judged: action is update_preserve_judged", body.get("action") == "update_preserve_judged")
    check("judged: accepted NOT clobbered (stays 1)", r["accepted"] == 1)
    check("judged: quality_score NOT clobbered (stays 0.91)", abs(r["quality_score"] - 0.91) < 1e-9)
    check("judged: observational cost still refreshed", abs(r["actual_cost_usd"] - 0.005) < 1e-9)

    # route_id -> model_used resolution via routes table.
    body, code = sprint3_endpoints.record_outcome(
        {"tag": "S3-BYROUTE", "task_class": "devops", "route_id": "forge:opencode:glm",
         "status": "completed", "cost_usd": 0.001}, db_path=path)
    check("route_id resolved to routes.model_id (model_used)",
          fetch_row(path, "S3-BYROUTE")["model_used"] == "z-ai/glm-4.6")


def test3_read_path_tier1(path):
    print("\n== TEST 3: read path - >=5 outcomes promote route+class to Tier-1 ==")
    model = "claude-haiku"
    tclass = "analysis"
    # 7 outcomes: 5 accepted (completed), 2 rejected (failed) -> observed rate 5/7.
    statuses = ["completed"] * 5 + ["failed"] * 2
    for i, st in enumerate(statuses):
        sprint3_endpoints.record_outcome(
            {"tag": f"S3-LOOP-{i}", "task_class": tclass, "model_id": model,
             "status": st, "cost_usd": 0.001}, db_path=path)
    # Observed accept-rate straight from the ledger (earlier tests also labelled this
    # model+class; the read path must reflect ALL real labels, so derive the rate
    # from the DB rather than hard-coding it).
    con = sqlite3.connect(path)
    n, rate = con.execute(
        "SELECT COUNT(*), AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) "
        "FROM dispatches WHERE model_used=? AND task_class=? AND accepted IS NOT NULL",
        (model, tclass)).fetchone()
    con.close()
    print(f"  observed labels for {model}/{tclass}: n={n} observed_rate={rate:.4f}")
    check("at least MIN_OBS(5) labelled outcomes present", n >= 5)

    # The analysis class gates to spine where claude-haiku is the only route, so its
    # predicted_success must equal the observed accept-rate (Tier-1, no warm-start).
    plan = route_select.select_route(RouteTask(tag="S3-READ", task_class=tclass,
                                               error_sensitivity="low"))
    print(f"  plan: route={plan.selected_route} model={plan.selected_model} "
          f"psucc={plan.predicted_success} floor={plan.quality_floor} err={plan.error}")
    check("a route was selected for analysis", bool(plan.selected_route) and plan.error is None)
    check("Tier-1 observed accept-rate reflected in predicted_success",
          abs(plan.predicted_success - round(rate, 3)) < 1e-3)


def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_sprint3_")
    os.close(fd)
    build_db(path)
    # select_route reads the module-global DB_PATH; in production that IS the same
    # argos.db classify_and_route/record_outcome write to, so point it at the temp db
    # for the whole run (mirrors the prod invariant db_path == route_select.DB_PATH).
    orig_db = route_select.DB_PATH
    route_select.DB_PATH = path
    try:
        test0_refactor_shared_seam()
        test1_classify_and_route(path)
        test2_outcome_upsert(path)
        test3_read_path_tier1(path)
    finally:
        route_select.DB_PATH = orig_db
        os.unlink(path)

    print()
    if not PASS:
        print("SMOKE SPRINT3: FAIL")
        sys.exit(1)
    print("SMOKE SPRINT3: ALL PASS")


if __name__ == "__main__":
    main()
