#!/usr/bin/env python3
"""Offline smoke test for U4-001: panel/champion warm-start priors (shadow-only).

Runnable: `python3 tests/smoke_u4.py`. Builds a temp sqlite db (no network, never
touches the real argos.db) and exercises route_priors_dynamic +
route_select._predicted_success against seeded panel_decisions / champions /
dispatches rows.

Guards proven here:
  * panel-recommended route with NO observed data -> prior boosted, reason names panel.
  * champion route (DEFENSIBLY-mapped class) with no data -> small champion boost.
  * route+class WITH >=5 observed labels -> OBSERVED rate, boost NOT applied (the
    critical "real data wins" guard).
  * malformed / empty panel JSON -> no raise, falls back to unboosted.
  * boost is hard-capped at 0.95 and never drops below base.
  * an unmapped bake-off class ("reasoning") is skipped (no wrong mapping).
"""
import os, sys, sqlite3, tempfile

# import modules under test (repo root is the parent of this tests/ dir)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_priors
import route_priors_dynamic as rpd
import route_select
from route_select import RouteTask

PASS = True
def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


def make_db(path, panel_recommendation_json, newest_panel_json=None):
    """Build a temp db. panel_recommendation_json is the OLDER (valid) panel;
    newest_panel_json (optional) is written with a later week_start (e.g. malformed)."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE routes (
        route_id TEXT PRIMARY KEY, backend TEXT, tool TEXT, access_path TEXT,
        model_id TEXT, cost_mode TEXT, enabled INTEGER DEFAULT 1)""")
    con.executemany(
        "INSERT INTO routes VALUES (?,?,?,?,?,?,?)",
        [
            # panel-recommended route for code_generation (a codex CLI route)
            ("forge:codex-cli", "forge", "codex-cli", "native", "openai/gpt-5.3-codex", "sunk", 1),
            # active-champion route for code-debug -> debugging (an Opus route)
            ("spine:debug",     "spine", "opencode",  "api",    "anthropic/claude-opus-4.5", "api", 1),
            # route that will carry >=5 observed labels for test_unit
            ("forge:obs",       "forge", "opencode",  "api",    "openai/gpt-5.4", "api", 1),
            # route whose model is the (unmapped) "reasoning" champion
            ("spine:reason",    "spine", "opencode",  "api",    "google/gemini-3.1-pro-preview", "api", 1),
        ],
    )
    con.execute("""CREATE TABLE panel_decisions (
        panel_id INTEGER PRIMARY KEY AUTOINCREMENT, week_start DATE,
        recommendation TEXT, consensus_score REAL, andy_decision TEXT,
        applied_at TIMESTAMP, notes TEXT)""")
    # older valid panel (andy_decision NULL == advisory, per ground truth)
    con.execute(
        "INSERT INTO panel_decisions (week_start, recommendation, consensus_score, "
        "andy_decision, applied_at, notes) VALUES (?,?,?,?,?,?)",
        ("2026-06-01", panel_recommendation_json, 0.8, None, None, "smoke older"))
    if newest_panel_json is not None:
        con.execute(
            "INSERT INTO panel_decisions (week_start, recommendation, consensus_score, "
            "andy_decision, applied_at, notes) VALUES (?,?,?,?,?,?)",
            ("2026-06-14", newest_panel_json, 0.8, None, None, "smoke newest"))
    con.execute("""CREATE TABLE champions (
        champion_id INTEGER PRIMARY KEY AUTOINCREMENT, task_class TEXT, model_id TEXT,
        since_round_id INTEGER, promoted_at TIMESTAMP, dethroned_at TIMESTAMP)""")
    con.executemany(
        "INSERT INTO champions (task_class, model_id, since_round_id, promoted_at, dethroned_at) "
        "VALUES (?,?,?,?,?)",
        [
            # active champion for bake-off code-debug -> maps to debugging
            ("code-debug", "anthropic/claude-opus-4.5", 7, "2026-06-10T00:00:00", None),
            # active champion for bake-off "reasoning" -> NO defensible map (must be skipped)
            ("reasoning",  "google/gemini-3.1-pro-preview", 7, "2026-06-10T00:00:00", None),
        ],
    )
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, model_used TEXT, task_class TEXT, accepted INTEGER)""")
    # >=5 labelled rows for forge:obs's model on test_unit: 3 accepted / 2 not => 0.6
    con.executemany(
        "INSERT INTO dispatches VALUES (?,?,?,?)",
        [("o1", "openai/gpt-5.4", "test_unit", 1),
         ("o2", "openai/gpt-5.4", "test_unit", 1),
         ("o3", "openai/gpt-5.4", "test_unit", 1),
         ("o4", "openai/gpt-5.4", "test_unit", 0),
         ("o5", "openai/gpt-5.4", "test_unit", 0)],
    )
    con.commit()
    con.close()


# Panel recommendation JSON. code_generation -> recommends forge:codex-cli.
# test_unit -> recommends forge:obs (this would boost, but observed data must win).
# refactoring -> recommended_q above the cap (tests the 0.95 clamp).
# formatting -> pareto entry with a LOW q (tests "never below base").
PANEL_JSON = (
    '{'
    '"code_generation": {"floor":0.85,"recommended":"forge:codex-cli",'
    '"recommended_q":0.90,"any_clears":true,'
    '"pareto":[{"route":"forge:codex-cli","eff":0.0005,"q":0.90}]},'
    '"test_unit": {"floor":0.70,"recommended":"forge:obs","recommended_q":0.95,'
    '"any_clears":true,"pareto":[{"route":"forge:obs","eff":0.0003,"q":0.95}]},'
    '"refactoring": {"floor":0.80,"recommended":"forge:codex-cli","recommended_q":0.99,'
    '"any_clears":true,"pareto":[{"route":"forge:codex-cli","eff":0.0005,"q":0.99}]},'
    '"formatting": {"floor":0.65,"recommended":"spine:debug","recommended_q":0.80,'
    '"any_clears":true,"pareto":[{"route":"forge:codex-cli","eff":0.0005,"q":0.40}]}'
    '}'
)

print("== U4-001 smoke: panel/champion warm-start priors (shadow-only) ==")

fd, dbpath = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    # newest panel row is deliberately MALFORMED -> module must skip it and fall
    # back to the older valid panel (resilience exercised on every panel read).
    make_db(dbpath, PANEL_JSON, newest_panel_json='{ this is not valid json ')
    con = sqlite3.connect(dbpath)
    con.row_factory = sqlite3.Row

    def route(rid):
        return con.execute("SELECT * FROM routes WHERE route_id=?", (rid,)).fetchone()

    # --- Case 1: panel-recommended route, NO observed data -> boost + panel reason
    r_panel = route("forge:codex-cli")
    seed_cg = route_priors.seed_prior(r_panel["model_id"], r_panel["tool"], "medium")
    psucc_cg, note_cg = route_select._predicted_success(
        con, r_panel, RouteTask(task_class="code_generation", error_sensitivity="medium"), 0.70)
    check("panel-recommended: prior >= unboosted seed (boost applied)", psucc_cg >= seed_cg)
    check("panel-recommended: strictly boosted above seed", psucc_cg > seed_cg)
    check("panel-recommended: reason mentions panel",
          note_cg is not None and "panel" in note_cg)

    # --- Case 2: champion route (mapped code-debug -> debugging), no data -> champ boost
    r_champ = route("spine:debug")
    seed_db = route_priors.seed_prior(r_champ["model_id"], r_champ["tool"], "medium")
    psucc_db, note_db = route_select._predicted_success(
        con, r_champ, RouteTask(task_class="debugging", error_sensitivity="medium"), 0.70)
    check("champion: prior >= unboosted seed (boost applied)", psucc_db >= seed_db)
    check("champion: strictly boosted above seed", psucc_db > seed_db)
    check("champion: reason mentions champion",
          note_db is not None and "champion" in note_db)

    # --- Case 3 (CRITICAL): >=5 observed labels -> OBSERVED rate, NO boost over real data
    r_obs = route("forge:obs")
    psucc_obs, note_obs = route_select._predicted_success(
        con, r_obs, RouteTask(task_class="test_unit", error_sensitivity="medium"), 0.70)
    check("observed tier-1: returns observed rate 0.6 unchanged", abs(psucc_obs - 0.6) < 1e-9)
    check("observed tier-1: NO boost note (real data wins, even though panel recommends it)",
          note_obs is None)

    # --- Case 4: hard cap at 0.95 (recommended_q=0.99 must clamp)
    base_rf = 0.80
    boosted_rf, reason_rf = rpd.champion_panel_boost(con, r_panel, "refactoring", base_rf)
    check("cap: boost never exceeds 0.95", boosted_rf <= 0.95)
    check("cap: clamped exactly to 0.95 for recommended_q=0.99", abs(boosted_rf - 0.95) < 1e-9)
    check("cap: reason mentions panel", reason_rf is not None and "panel" in reason_rf)

    # --- Case 5: never below base (pareto q below base -> stays at base, boost-only)
    base_fmt = 0.86
    boosted_fmt, reason_fmt = rpd.champion_panel_boost(con, r_panel, "formatting", base_fmt)
    check("boost-only: never goes below base (low pareto q)", boosted_fmt >= base_fmt)
    check("boost-only: stays exactly at base when target < base", abs(boosted_fmt - base_fmt) < 1e-9)

    # --- Case 6: unmapped bake-off class "reasoning" is SKIPPED (no wrong mapping)
    check("unmapped: load_champion_model('reasoning') is None",
          rpd.load_champion_model(con, "reasoning") is None)
    r_reason = route("spine:reason")
    base_rsn = 0.70
    boosted_rsn, reason_rsn = rpd.champion_panel_boost(con, r_reason, "reasoning", base_rsn)
    check("unmapped: no boost applied for reasoning", abs(boosted_rsn - base_rsn) < 1e-9)
    check("unmapped: no reason emitted for reasoning", reason_rsn is None)

    # --- Case 7: defensible champion mapping is wired and correct
    check("mapping: code-debug -> debugging champion resolves",
          rpd.load_champion_model(con, "debugging") == "anthropic/claude-opus-4.5")

    con.close()
finally:
    os.remove(dbpath)

# --- Case 8: malformed-only / empty panel must not raise; falls back to unboosted
fd2, dbpath2 = tempfile.mkstemp(suffix=".db")
os.close(fd2)
try:
    make_db(dbpath2, '{ totally broken json :::')  # the ONLY panel row is malformed
    con2 = sqlite3.connect(dbpath2)
    con2.row_factory = sqlite3.Row
    r = con2.execute("SELECT * FROM routes WHERE route_id=?", ("forge:codex-cli",)).fetchone()
    raised = False
    try:
        # no champion maps to code_generation either -> must be a clean no-op
        b, why = rpd.champion_panel_boost(con2, r, "code_generation", 0.77)
    except Exception as exc:
        raised = True
        b, why = None, repr(exc)
    check("malformed panel: does NOT raise", not raised)
    check("malformed panel: falls back to unboosted base", b == 0.77)
    check("malformed panel: no boost reason", why is None)
    # load_panel_prior itself tolerates the malformed row
    check("malformed panel: load_panel_prior returns None (no raise)",
          rpd.load_panel_prior(con2, "code_generation") is None)
    con2.close()
finally:
    os.remove(dbpath2)

print()
print("SMOKE U4: ALL PASS" if PASS else "SMOKE U4: FAIL")
sys.exit(0 if PASS else 1)
