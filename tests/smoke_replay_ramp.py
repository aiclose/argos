#!/usr/bin/env python3
"""Offline smoke for CHG-P9-050 PART 3: the replay usage ledger RAMPS the codex
bucket shadow price end-to-end through select_route.

Runnable: `python3 tests/smoke_replay_ramp.py`. No network, never touches the real
argos.db. Builds a temp db with a flat-rate codex-like sunk route (bucket
codex-oauth) competing in the FORGE backend against two per-token routes (a cheap
floor-clearing lane and a dearer one that sets the eta anchor), then replays the
SAME codeable task repeated within one calendar day. Proves:

  * SMALL CAP (--codex-cap 3): codex wins the first ~3 dispatches (its ration is
    not yet ramping), then later identical dispatches SPILL to the cheap per-token
    lane (codex effective_cost has risen above the per-token cash). spills > 0.
  * LARGE CAP (--codex-cap 100000): codex wins throughout, NO spill.
  * The verdict + peak reporting reflect whether the cap bound.
  * NO LEAK: cost overrides are None after each run.
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cost as costmod  # noqa: E402
import route_select  # noqa: E402
import replay_shadow  # noqa: E402

PASS = True
CODEX_ROUTE = "forge:codex-cli"
CHEAP_ROUTE = "forge:opencode:cheap"
DEAR_ROUTE = "forge:opencode:dear"
N_DISPATCHES = 6


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
        "cost_mode, enabled, healthcheck_type, last_health, quota_bucket) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            # sunk flat-rate codex lane -> codex-oauth bucket (the rationed one)
            (CODEX_ROUTE, "forge", "codex", "codex-oauth", "codex-cli-model",
             "sunk", 1, "cli-smoke", "ok", "codex-oauth"),
            # cheap per-token lane: clears the floor, sets the spill target
            (CHEAP_ROUTE, "forge", "opencode", "openrouter", "cheap-model",
             "per_token", 1, "cli-smoke", "ok", None),
            # dearer per-token lane: makes the median (eta) anchor > cheap cash
            (DEAR_ROUTE, "forge", "opencode", "openrouter", "dear-model",
             "per_token", 1, "cli-smoke", "ok", None),
        ],
    )
    con.execute("CREATE TABLE task_classes (class_id TEXT PRIMARY KEY, default_quality_floor REAL)")
    con.execute("INSERT INTO task_classes VALUES ('code_implementation', 0.5)")
    con.execute("CREATE TABLE model_prices (model_id TEXT PRIMARY KEY, "
                "input_per_1m_usd REAL, output_per_1m_usd REAL, request_overhead_usd REAL)")
    con.executemany(
        "INSERT INTO model_prices VALUES (?,?,?,?)",
        [("cheap-model", 0.2, 1.0, 0.0),    # cash ~ $0.0023 at 4000/1500 tokens
         ("dear-model", 1.0, 4.0, 0.0)],    # cash ~ $0.0100  -> eta = median = this
    )
    # cost.capacity_cost reads route_capacity for sunk lanes; an empty table is fine.
    con.execute("CREATE TABLE route_capacity (route_id TEXT, window TEXT, limit_units REAL, "
                "used_units REAL, reserve_target REAL, lambda_w REAL, "
                "window_length_sec REAL, resets_at TEXT)")
    # codex-oauth row with all caps NULL, like live; --codex-cap supplies the ration.
    con.execute("CREATE TABLE quota_caps (bucket TEXT PRIMARY KEY, daily_requests_cap REAL, "
                "daily_tokens_cap REAL, daily_cost_cap_usd REAL)")
    con.execute("INSERT INTO quota_caps VALUES ('codex-oauth', NULL, NULL, NULL)")
    con.execute("CREATE TABLE quota_usage (bucket TEXT, day TEXT, requests INTEGER, "
                "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)")
    # N identical codeable dispatches, same calendar day, ascending ts.
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, ts TIMESTAMP, task_class TEXT, model_used TEXT,
        actual_cost_usd REAL, accepted INTEGER, error_sensitivity TEXT,
        actual_input_tokens INTEGER, actual_output_tokens INTEGER)""")
    con.executemany(
        "INSERT INTO dispatches (dispatch_id, ts, task_class, model_used, actual_cost_usd, "
        "accepted, error_sensitivity, actual_input_tokens, actual_output_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (f"costlog-r{i}", f"2026-06-20T10:00:{i:02d}", "code_implementation",
             "codex-cli-model", 0.05, 1, "low", None, None)
            for i in range(1, N_DISPATCHES + 1)
        ],
    )
    con.commit()
    con.close()


def run(path, codex_cap):
    orig = route_select.DB_PATH
    route_select.DB_PATH = path
    try:
        return replay_shadow.build_replay(db_path=path, codex_cap=codex_cap)
    finally:
        route_select.DB_PATH = orig


def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_ramp_")
    os.close(fd)
    build_db(path)
    try:
        # --- SMALL CAP: codex rations after ~3 picks, later dispatches spill ---
        res = run(path, codex_cap=3)
        picks = [(r["dispatch_id"], r["argos_model"]) for r in res["rows"]]
        print(f"\n  [--codex-cap 3] picks: {picks}")
        print(f"  spills={res['spills']} verdict={res['verdict']}")
        codex_picks = [r for r in res["rows"] if r["argos_model"] == "codex-cli-model"]
        spill_picks = [r for r in res["rows"] if r["argos_model"] == "cheap-model"]
        check("small cap: codex won the EARLY dispatches", len(codex_picks) >= 1)
        check("small cap: the first dispatch went to codex",
              res["rows"][0]["argos_model"] == "codex-cli-model")
        check("small cap: later identical dispatches SPILLED to the cheap per-token lane",
              len(spill_picks) >= 1)
        check("small cap: the LAST dispatch is NOT codex (price ramped past it)",
              res["rows"][-1]["argos_model"] == "cheap-model")
        check("small cap: spill count > 0", res["spills"] > 0)
        check("small cap: codex peak usage <= cap (ration held)",
              res["peak_usage"].get("codex-oauth", {}).get("peak", 0) <= 3)
        check("small cap: verdict says the cap BOUND", "cap bound" in res["verdict"])
        check("small cap: NO override leak (usage)", costmod._BUCKET_USAGE_OVERRIDE is None)
        check("small cap: NO override leak (cap)", costmod._BUCKET_CAP_OVERRIDE is None)
        # codex effective_cost must have RISEN: early codex eff << late spill eff target
        codex_effs = [r["argos_effective_cost"] for r in codex_picks]
        check("small cap: early codex effective_cost is the cheap floor (~nominal)",
              codex_effs and min(codex_effs) < 0.001)

        # --- LARGE CAP: codex never rations, wins throughout ---
        res_big = run(path, codex_cap=100000)
        picks_big = [r["argos_model"] for r in res_big["rows"]]
        print(f"\n  [--codex-cap 100000] picks: {picks_big}")
        print(f"  spills={res_big['spills']} verdict={res_big['verdict']}")
        check("large cap: codex won EVERY dispatch (no ramp)",
              all(m == "codex-cli-model" for m in picks_big))
        check("large cap: spill count == 0", res_big["spills"] == 0)
        check("large cap: verdict says the cap did NOT bind", "did NOT bind" in res_big["verdict"])
        check("large cap: NO override leak (usage)", costmod._BUCKET_USAGE_OVERRIDE is None)
        check("large cap: NO override leak (cap)", costmod._BUCKET_CAP_OVERRIDE is None)

        # --- renderer still emits a hyphen-only honesty table ---
        md = replay_shadow.render_markdown(res)
        check("markdown carries the capacity-honesty section", "Capacity honesty" in md)
        check("markdown carries a VERDICT line", "VERDICT:" in md)
        check("markdown uses hyphens, no emdashes", "—" not in md)
    finally:
        os.unlink(path)

    print()
    if not PASS:
        print("SMOKE REPLAY-RAMP: FAIL")
        sys.exit(1)
    print("SMOKE REPLAY-RAMP: ALL PASS")


if __name__ == "__main__":
    main()
