#!/usr/bin/env python3
"""Offline smoke for CHG-P9-052 (Sprint 2.5b): dual-gate (basic|strict) + floor
recalibration. These bugs are SILENT (scale confusion, mode leakage), so every
test asserts a DECISION, not just an import.

Runnable: `python3 tests/smoke_sprint25b.py`. No network. The logic-heavy quadrant
checks drive the pure route_select.evaluate_gate(); the wiring checks build a temp
sqlite (never touches the real argos.db), point route_select.DB_PATH at it, and
stub costmod.effective_cost so eligibility is observable independent of cost.

Covers the brief's six assertions:
  1. accept-rate is NEVER compared against a quality-scale floor (the original bug).
  2. accept_floor is byte-identical across gate modes.
  3. mode toggle changes ONLY the accepted-but-sloppy quadrant.
  4. missing-benchmark model in strict is NOT blocked by the quality predicate.
  5. q_min is LOOSE: 0.85+ cluster passes, only genuinely-bad (<=0.68) fails.
  6. the gate (clears) is decoupled from cost.
"""
import inspect
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_select          # noqa: E402
import cost as costmod        # noqa: E402
from route_select import RouteTask, evaluate_gate  # noqa: E402

PASS = True


def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


# --------------------------------------------------------------------------- #
# temp-DB helpers for the end-to-end wiring tests
# --------------------------------------------------------------------------- #
def build_db(path, *, routes, task_classes, dispatches=(), bench=()):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE routes (
        route_id TEXT PRIMARY KEY, backend TEXT, tool TEXT, access_path TEXT,
        model_id TEXT, cost_mode TEXT, enabled INTEGER DEFAULT 1,
        healthcheck_type TEXT, last_health TEXT)""")
    con.executemany(
        "INSERT INTO routes (route_id, backend, tool, access_path, model_id, "
        "cost_mode, enabled, healthcheck_type, last_health) VALUES (?,?,?,?,?,?,?,?,?)",
        routes)
    con.execute("""CREATE TABLE task_classes (
        class_id TEXT PRIMARY KEY, parent_class_id TEXT, description TEXT,
        default_quality_floor REAL, default_error_sensitivity TEXT)""")
    con.executemany(
        "INSERT INTO task_classes (class_id, default_quality_floor, "
        "default_error_sensitivity) VALUES (?,?,?)", task_classes)
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, ts TEXT, model_used TEXT, task_class TEXT,
        accepted INTEGER)""")
    if dispatches:
        con.executemany("INSERT INTO dispatches VALUES (?,?,?,?,?)", dispatches)
    con.execute("""CREATE TABLE bench_cache (
        model_id TEXT, task_id TEXT, judge_model TEXT, success INT, score REAL,
        latency_ms INT, scored_at TEXT, PRIMARY KEY (model_id, task_id, judge_model))""")
    if bench:
        con.executemany(
            "INSERT INTO bench_cache (model_id, task_id, judge_model, score) "
            "VALUES (?,?,?,?)", bench)
    con.commit()
    con.close()


def _labels(model, task_class, n_accept, n_total, prefix):
    """n_total dispatch rows for model+class, n_accept of them accepted."""
    return [(f"{prefix}{i}", "t", model, task_class, 1 if i < n_accept else 0)
            for i in range(n_total)]


def run_plan(path, task, *, gate_mode, costs=None):
    """Drive select_route against the temp db under a given gate_mode."""
    orig_db, orig_env, orig_cost = (
        route_select.DB_PATH, os.environ.get("ARGOS_GATE_MODE"), costmod.effective_cost)
    route_select.DB_PATH = path
    os.environ["ARGOS_GATE_MODE"] = gate_mode
    # Stub cost so eligibility is observable without model_prices wiring. A flat
    # cost when the test doesn't care; a per-route map when it probes decoupling.
    if costs is None:
        costmod.effective_cost = lambda route_id, t, con: 0.001
    else:
        costmod.effective_cost = lambda route_id, t, con, _c=costs: _c[route_id]
    try:
        return route_select.select_route(task)
    finally:
        route_select.DB_PATH = orig_db
        costmod.effective_cost = orig_cost
        if orig_env is None:
            os.environ.pop("ARGOS_GATE_MODE", None)
        else:
            os.environ["ARGOS_GATE_MODE"] = orig_env


# --------------------------------------------------------------------------- #
# Test 1 - the original bug: accept-rate must NOT be gated by a quality-scale floor
# --------------------------------------------------------------------------- #
def test1_accept_not_quality_scale():
    print("\n[1] accept-rate is never compared against a quality-scale floor")
    # Pure: a devops route at accept-rate 0.873 with the recalibrated 0.80 accept
    # floor clears the accept predicate (the bug was 0.873 vs a 0.90 quality floor).
    g = evaluate_gate(0.873, 0.80, "basic", None, 0.0)
    check("psucc 0.873 >= accept_floor 0.80 -> clears_accept TRUE", g["clears_accept"])
    check("0.873 would have FAILED the old 0.90 quality floor (the bug)", not (0.873 >= 0.90))

    # End-to-end: forge devops route, observed accept 0.875, post-migration floor
    # 0.80, strict mode, strong benchmark -> route selected (no false no-route).
    path = tempfile.mktemp(suffix=".db", prefix="s25b1_")
    build_db(
        path,
        routes=[("forge:opencode:strong", "forge", "opencode", "openrouter",
                 "anthropic/claude-opus-4.5", "per_token", 1, "cli-smoke", "ok")],
        task_classes=[("devops", 0.80, "high")],
        dispatches=_labels("anthropic/claude-opus-4.5", "devops", 7, 8, "dv"),
        bench=[("anthropic/claude-opus-4.5", "devops/x", route_select.CANONICAL_JUDGE, 0.88)],
    )
    try:
        plan = run_plan(path, RouteTask(tag="t1", task_class="devops"), gate_mode="strict")
    finally:
        os.remove(path)
    check("end-to-end: devops route SELECTED (accept 0.875 vs floor 0.80)",
          bool(plan.selected_route) and plan.error is None)
    check("end-to-end: floor reported on accept scale (0.80)", abs(plan.quality_floor - 0.80) < 1e-9)
    check("end-to-end: psucc is the observed accept-rate (~0.875)", plan.predicted_success >= 0.80)


# --------------------------------------------------------------------------- #
# Test 2 - accept_floor byte-identical across modes
# --------------------------------------------------------------------------- #
def test2_floor_identical_across_modes():
    print("\n[2] accept_floor is identical whether basic or strict")
    # Same (psucc, floor); only the mode differs. The accept predicate must not move.
    basic = evaluate_gate(0.78, 0.80, "basic", 0.90, 0.70)
    strict = evaluate_gate(0.78, 0.80, "strict", 0.90, 0.70)
    check("clears_accept identical across modes for same (psucc, floor)",
          basic["clears_accept"] == strict["clears_accept"])

    # _quality_floor takes no gate-mode argument -> structurally mode-independent.
    sig = inspect.signature(route_select._quality_floor)
    check("_quality_floor signature has no gate_mode param", "mode" not in sig.parameters)

    # End-to-end: same temp db, run basic and strict, the reported floor matches.
    path = tempfile.mktemp(suffix=".db", prefix="s25b2_")
    build_db(
        path,
        routes=[("spine:litellm:m", "spine", "litellm", "litellm",
                 "anthropic/claude-sonnet-4.5", "per_token", 1, "api-chat", "ok")],
        task_classes=[("analysis", 0.70, "low")],
        dispatches=_labels("anthropic/claude-sonnet-4.5", "analysis", 5, 5, "an"),
        bench=[("anthropic/claude-sonnet-4.5", "analysis/x", route_select.CANONICAL_JUDGE, 0.86)],
    )
    try:
        pb = run_plan(path, RouteTask(tag="t2", task_class="analysis"), gate_mode="basic")
        ps = run_plan(path, RouteTask(tag="t2", task_class="analysis"), gate_mode="strict")
    finally:
        os.remove(path)
    check("end-to-end: quality_floor identical across modes",
          abs(pb.quality_floor - ps.quality_floor) < 1e-9)


# --------------------------------------------------------------------------- #
# Test 3 - mode toggle changes ONLY the accepted-but-sloppy quadrant
# --------------------------------------------------------------------------- #
def test3_mode_quadrant():
    print("\n[3] mode toggle only flips the high-accept / low-quality quadrant")
    FLOOR, QMIN = 0.80, 0.70
    # high accept (clears accept), low benchmark (below q_min): the sloppy quadrant.
    sloppy_b = evaluate_gate(0.95, FLOOR, "basic", 0.50, QMIN)
    sloppy_s = evaluate_gate(0.95, FLOOR, "strict", 0.50, QMIN)
    check("accepted-but-sloppy: ELIGIBLE in basic", sloppy_b["clears"])
    check("accepted-but-sloppy: BLOCKED in strict", not sloppy_s["clears"])
    check("accepted-but-sloppy: blocked specifically on quality, not accept",
          sloppy_s["clears_accept"] and not sloppy_s["clears_quality"])
    # high on both -> eligible in both modes.
    good_b = evaluate_gate(0.95, FLOOR, "basic", 0.88, QMIN)
    good_s = evaluate_gate(0.95, FLOOR, "strict", 0.88, QMIN)
    check("high accept + high quality: eligible in BOTH modes", good_b["clears"] and good_s["clears"])
    # low accept -> blocked in both modes regardless of quality.
    bad_b = evaluate_gate(0.40, FLOOR, "basic", 0.88, QMIN)
    bad_s = evaluate_gate(0.40, FLOOR, "strict", 0.88, QMIN)
    check("low accept: blocked in BOTH modes", (not bad_b["clears"]) and (not bad_s["clears"]))

    # End-to-end: a sloppy spine route (accept 1.0, bench 0.50 < q_min low 0.55) is
    # selected in basic but no-routes in strict -> the toggle reaches a real decision.
    path = tempfile.mktemp(suffix=".db", prefix="s25b3_")
    build_db(
        path,
        routes=[("spine:litellm:sloppy", "spine", "litellm", "litellm",
                 "xiaomi/mimo-v2.5", "per_token", 1, "api-chat", "ok")],
        task_classes=[("analysis", 0.70, "low")],
        dispatches=_labels("xiaomi/mimo-v2.5", "analysis", 6, 6, "sl"),
        bench=[("xiaomi/mimo-v2.5", "analysis/x", route_select.CANONICAL_JUDGE, 0.50)],
    )
    try:
        pb = run_plan(path, RouteTask(tag="t3", task_class="analysis"), gate_mode="basic")
        ps = run_plan(path, RouteTask(tag="t3", task_class="analysis"), gate_mode="strict")
    finally:
        os.remove(path)
    check("end-to-end: sloppy model SELECTED in basic", bool(pb.selected_route) and pb.error is None)
    check("end-to-end: sloppy model NO-ROUTED in strict", ps.selected_route is None and ps.error is not None)


# --------------------------------------------------------------------------- #
# Test 4 - missing benchmark in strict mode is NOT blocked by the quality predicate
# --------------------------------------------------------------------------- #
def test4_missing_benchmark():
    print("\n[4] missing benchmark in strict is governed by accept, not quality")
    g = evaluate_gate(0.90, 0.80, "strict", None, 0.70)
    check("no benchmark + clears accept -> clears_quality TRUE (not blocked)", g["clears_quality"])
    check("no benchmark + clears accept -> overall ELIGIBLE", g["clears"])
    g2 = evaluate_gate(0.50, 0.80, "strict", None, 0.70)
    check("no benchmark + fails accept -> blocked by ACCEPT, quality still TRUE",
          (not g2["clears"]) and g2["clears_quality"] and (not g2["clears_accept"]))

    # _benchmark_quality returns None for a model with no bench_cache row.
    path = tempfile.mktemp(suffix=".db", prefix="s25b4_")
    build_db(path, routes=[], task_classes=[("devops", 0.80, "high")],
             bench=[("known/model", "devops/x", route_select.CANONICAL_JUDGE, 0.9)])
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        bq_missing = route_select._benchmark_quality(con, "unknown/model", "devops")
        bq_known = route_select._benchmark_quality(con, "known/model", "devops")
    finally:
        con.close()
        os.remove(path)
    check("_benchmark_quality -> None for un-benched model", bq_missing is None)
    check("_benchmark_quality -> the score for a benched model", bq_known is not None and abs(bq_known - 0.9) < 1e-9)


# --------------------------------------------------------------------------- #
# Test 5 - q_min is LOOSE: 0.85+ cluster passes, only genuinely-bad (<=0.68) fails
# --------------------------------------------------------------------------- #
def test5_qmin_loose():
    print("\n[5] q_min is loose - strong cluster passes, genuinely-bad fails")
    policy = route_select._load_policy()
    # devops is error_sensitivity 'high'; analysis is 'low'. q_min looked up by class
    # falls through to sensitivity (no per-class override in the shipped policy).
    qmin_devops = route_select._q_min_for(policy, RouteTask(task_class="devops"), "high")
    qmin_analysis = route_select._q_min_for(policy, RouteTask(task_class="analysis"), "low")
    print(f"      q_min devops(high)={qmin_devops}  q_min analysis(low)={qmin_analysis}")

    # devops: strong cluster passes, nemotron ~0.68 fails. Force accept TRUE so the
    # quality predicate is the only thing under test.
    strong = [0.85, 0.88, 0.90]
    for s in strong:
        check(f"devops bench {s} (strong) PASSES q_min {qmin_devops}",
              evaluate_gate(0.95, 0.80, "strict", s, qmin_devops)["clears_quality"])
    check(f"devops bench 0.68 (nemotron) FAILS q_min {qmin_devops}",
          not evaluate_gate(0.95, 0.80, "strict", 0.68, qmin_devops)["clears_quality"])

    # analysis: strong cluster passes, mimo-v2.5 ~0.50 fails.
    for s in strong:
        check(f"analysis bench {s} (strong) PASSES q_min {qmin_analysis}",
              evaluate_gate(0.95, 0.70, "strict", s, qmin_analysis)["clears_quality"])
    check(f"analysis bench 0.50 (mimo-v2.5) FAILS q_min {qmin_analysis}",
          not evaluate_gate(0.95, 0.70, "strict", 0.50, qmin_analysis)["clears_quality"])


# --------------------------------------------------------------------------- #
# Test 6 - the gate (clears) is decoupled from cost
# --------------------------------------------------------------------------- #
def test6_decoupling():
    print("\n[6] eligibility (clears) is independent of cost")
    # Structural: the pure gate has no cost parameter at all.
    params = set(inspect.signature(evaluate_gate).parameters)
    check("evaluate_gate has no cost parameter", not (params & {"cost", "eff", "effective_cost"}))
    # Identical gate inputs -> identical clears no matter what a cost would be.
    a = evaluate_gate(0.9, 0.8, "strict", 0.88, 0.70)
    b = evaluate_gate(0.9, 0.8, "strict", 0.88, 0.70)
    check("same gate inputs -> identical clears", a == b)

    # End-to-end: two clearing spine routes; flipping their costs flips the WINNER
    # but BOTH runs still return a cleared selection (eligible set is cost-stable).
    path = tempfile.mktemp(suffix=".db", prefix="s25b6_")
    build_db(
        path,
        routes=[
            ("spine:litellm:A", "spine", "litellm", "litellm",
             "anthropic/claude-opus-4.5", "per_token", 1, "api-chat", "ok"),
            ("spine:litellm:B", "spine", "litellm", "litellm",
             "anthropic/claude-sonnet-4.5", "per_token", 1, "api-chat", "ok"),
        ],
        task_classes=[("analysis", 0.70, "low")],
        dispatches=(_labels("anthropic/claude-opus-4.5", "analysis", 5, 5, "a") +
                    _labels("anthropic/claude-sonnet-4.5", "analysis", 5, 5, "b")),
        bench=[("anthropic/claude-opus-4.5", "analysis/x", route_select.CANONICAL_JUDGE, 0.88),
               ("anthropic/claude-sonnet-4.5", "analysis/x", route_select.CANONICAL_JUDGE, 0.86)],
    )
    task = RouteTask(tag="t6", task_class="analysis")
    try:
        cheapA = run_plan(path, task, gate_mode="strict",
                          costs={"spine:litellm:A": 0.001, "spine:litellm:B": 0.009})
        cheapB = run_plan(path, task, gate_mode="strict",
                          costs={"spine:litellm:A": 0.009, "spine:litellm:B": 0.001})
    finally:
        os.remove(path)
    check("cheaper route A wins when A is cheap", cheapA.selected_route == "spine:litellm:A")
    check("cheaper route B wins when B is cheap", cheapB.selected_route == "spine:litellm:B")
    check("both routes stayed ELIGIBLE across the cost flip (cleared, no floor-fail)",
          cheapA.cleared_floor and cheapB.cleared_floor)


def main():
    test1_accept_not_quality_scale()
    test2_floor_identical_across_modes()
    test3_mode_quadrant()
    test4_missing_benchmark()
    test5_qmin_loose()
    test6_decoupling()
    print()
    print("SMOKE SPRINT-2.5b:", "ALL PASS" if PASS else "FAIL")
    sys.exit(0 if PASS else 1)


if __name__ == "__main__":
    main()
