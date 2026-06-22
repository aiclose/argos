#!/usr/bin/env python3
"""Offline smoke for ARGOS-S2 FIX 2b: replay_shadow build_replay.

Runnable: `python3 tests/smoke_replay.py`. Builds a temp sqlite db (no network,
never touches the real argos.db), points route_select at it, and drives
replay_shadow.build_replay() over a LIMIT 5 slice in-process - the same
select_route path /route-v2 uses.

Guards:
  * build_replay and main are callable.
  * a dry replay returns rows carrying the expected keys
    (task_class, realised_model, argos_model, delta_cost, would_switch, ...)
    without raising.
  * ineligible dispatches (no task_class / no realised model / no realised cost)
    are SKIPPED and counted, not emitted as evidence rows.
  * the markdown renderer produces a hyphen-only table without raising.
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_select  # noqa: E402
import replay_shadow  # noqa: E402

PASS = True
EXPECTED_KEYS = {"dispatch_id", "task_class", "realised_model", "realised_cost",
                 "argos_model", "argos_effective_cost", "delta_cost",
                 "would_switch", "note"}


def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


def build_db(path):
    con = sqlite3.connect(path)
    # routes: healthy spine + forge lanes, mirroring the live shape.
    con.execute("""CREATE TABLE routes (
        route_id TEXT PRIMARY KEY, backend TEXT, tool TEXT, access_path TEXT,
        model_id TEXT, cost_mode TEXT, enabled INTEGER DEFAULT 1,
        healthcheck_type TEXT, last_health TEXT, quota_bucket TEXT)""")
    con.executemany(
        "INSERT INTO routes (route_id, backend, tool, access_path, model_id, "
        "cost_mode, enabled, healthcheck_type, last_health) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("spine:litellm:claude-haiku", "spine", "litellm", "litellm",
             "claude-haiku", "per_token", 1, "api-chat", "ok"),
            ("spine:litellm:deepseek-v3", "spine", "litellm", "litellm",
             "deepseek-v3", "per_token", 1, "api-chat", "ok"),
            ("forge:opencode:glm", "forge", "opencode", "openrouter",
             "z-ai/glm-4.6", "per_token", 1, "cli-smoke", "ok"),
        ],
    )
    con.execute("CREATE TABLE task_classes (class_id TEXT PRIMARY KEY, default_quality_floor REAL)")
    con.executemany(
        "INSERT INTO task_classes (class_id, default_quality_floor) VALUES (?,?)",
        [("documentation", 0.5), ("analysis", 0.5), ("code_implementation", 0.5)],
    )
    con.execute("CREATE TABLE model_prices (model_id TEXT PRIMARY KEY, "
                "input_per_1m_usd REAL, output_per_1m_usd REAL, request_overhead_usd REAL)")
    con.executemany(
        "INSERT INTO model_prices VALUES (?,?,?,?)",
        [("claude-haiku", 0.8, 4.0, 0.0),
         ("deepseek-v3", 0.27, 1.1, 0.0),
         ("z-ai/glm-4.6", 0.5, 2.0, 0.0)],
    )
    # dispatches: the realised side. 'accepted' col is read by select_route.
    con.execute("""CREATE TABLE dispatches (
        dispatch_id TEXT PRIMARY KEY, task_class TEXT, model_used TEXT,
        actual_cost_usd REAL, accepted INTEGER, error_sensitivity TEXT,
        actual_input_tokens INTEGER, actual_output_tokens INTEGER)""")
    con.executemany(
        "INSERT INTO dispatches (dispatch_id, task_class, model_used, actual_cost_usd, "
        "accepted, error_sensitivity, actual_input_tokens, actual_output_tokens) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            # eligible rows (realised model deliberately NOT a route model -> would_switch)
            ("costlog-1", "documentation", "openai/gpt-5.4", 0.0200, 1, "low", 3000, 1200),
            ("costlog-2", "analysis", "openai/gpt-5.4", 0.0150, 1, "medium", None, None),
            ("costlog-3", "code_implementation", "z-ai/glm-4.6", 0.0090, 0, "high", 5000, 2000),
            # ineligible: must be SKIPPED, not emitted
            ("costlog-4", None, "openai/gpt-5.4", 0.0100, 1, "low", None, None),
            ("costlog-5", "documentation", None, 0.0100, 1, "low", None, None),
            ("costlog-6", "documentation", "openai/gpt-5.4", None, 1, "low", None, None),
        ],
    )
    con.commit()
    con.close()


def main():
    check("build_replay is callable", callable(replay_shadow.build_replay))
    check("main is callable", callable(replay_shadow.main))

    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_replay_")
    os.close(fd)
    build_db(path)
    orig_db = route_select.DB_PATH
    route_select.DB_PATH = path  # so select_route reads the fixture, not the real db
    try:
        result = replay_shadow.build_replay(db_path=path, limit=6)
    except Exception as e:
        print(f"  [FAIL] build_replay raised: {type(e).__name__}: {e}")
        route_select.DB_PATH = orig_db
        os.unlink(path)
        print("\nSMOKE REPLAY: FAIL")
        sys.exit(1)

    rows = result["rows"]
    print(f"\n  rows={len(rows)} skipped={result['skipped']} "
          f"skip_reasons={result['skip_reasons']} argos_errors={result['argos_errors']}")
    for r in rows:
        print(f"    {r['dispatch_id']}: realised={r['realised_model']} "
              f"argos={r['argos_model']} delta={r['delta_cost']} switch={r['would_switch']}")

    check("returns at least one evidence row", len(rows) >= 1)
    check("every row carries the expected keys",
          all(EXPECTED_KEYS.issubset(r.keys()) for r in rows))
    check("would_switch is always a bool",
          all(isinstance(r["would_switch"], bool) for r in rows))
    check("no ineligible dispatch leaked into the rows",
          all(r["dispatch_id"] not in ("costlog-4", "costlog-5", "costlog-6") for r in rows))
    check("the 3 ineligible rows were skipped + counted", result["skipped"] == 3)
    check("skip reasons cover all three gates",
          set(result["skip_reasons"]) == {"no_task_class", "no_realised_model", "no_realised_cost"})
    check("summary exposes headline pct_saved key", "pct_saved" in result["summary"])
    check("by_class aggregation is populated", len(result["by_class"]) >= 1)

    # The selected routes really come from the fixture (proves in-process select_route ran).
    argos_models = {r["argos_model"] for r in rows if r["argos_model"]}
    check("argos picked real fixture route models",
          argos_models.issubset({"claude-haiku", "deepseek-v3", "z-ai/glm-4.6"}) and bool(argos_models))

    # Renderer must not raise and must stay emdash-free.
    try:
        md = replay_shadow.render_markdown(result)
        check("render_markdown produced a table", "| task_class |" in md)
        check("markdown uses hyphens, no emdashes", "—" not in md)
    except Exception as e:
        check(f"render_markdown raised: {e}", False)

    route_select.DB_PATH = orig_db
    os.unlink(path)

    print()
    if not PASS:
        print("SMOKE REPLAY: FAIL")
        sys.exit(1)
    print("SMOKE REPLAY: ALL PASS")


if __name__ == "__main__":
    main()
