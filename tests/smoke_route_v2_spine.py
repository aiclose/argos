#!/usr/bin/env python3
"""Offline smoke test for ARGOS-S1 FIX 2: path-native health gate honours api-chat.

Runnable: `python3 tests/smoke_route_v2_spine.py`. Builds a temp sqlite db (no
network, never touches the real argos.db), points route_select at it, then drives
select_route() in-process (the same code /route-v2 calls).

Proves the gate fix:
  * spine:litellm routes whose health was set via their OWN LiteLLM access path
    (healthcheck_type='api-chat', last_health='ok') are now SELECTABLE. For
    task_class in {documentation, analysis, docs_explainer} -> backend=spine, a
    real LiteLLM alias is picked, and there is NO no_spine_route_available error.
  * NULL last_health is NEVER treated as healthy (the un-probed spine route is
    never selected).
  * api-chat with last_health != 'ok' is excluded.

REGRESSION (the gate change must not alter Forge routing):
  * task_class=code_implementation STILL returns a Forge (cli-smoke) route.

Before this fix, _route_is_cli_smoke_healthy() structurally excluded every
api-chat route, so the spine cases would have returned no_spine_route_available.
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_select  # noqa: E402
from route_select import RouteTask  # noqa: E402

PASS = True
SPINE_ALIASES = {"claude-haiku", "deepseek-v3", "or-auto"}


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
            # spine:litellm, probed OK via the LiteLLM path -> now selectable
            ("spine:litellm:claude-haiku", "spine", "litellm", "litellm",
             "claude-haiku", "per_token", 1, "api-chat", "ok"),
            ("spine:litellm:deepseek-v3", "spine", "litellm", "litellm",
             "deepseek-v3", "per_token", 1, "api-chat", "ok"),
            ("spine:litellm:or-auto", "spine", "litellm", "litellm",
             "or-auto", "per_token", 1, "api-chat", "ok"),
            # spine route never probed (NULL) -> must NEVER be selected
            ("spine:litellm:unprobed", "spine", "litellm", "litellm",
             "zdr-gpt-oss-120b", "per_token", 1, "api-chat", None),
            # spine route probed and FAILED -> excluded
            ("spine:litellm:dead", "spine", "litellm", "litellm",
             "zdr-deepseek-v4-flash", "per_token", 1, "api-chat", "fail:HTTPError"),
            # Forge route, healthy via its own CLI lane -> the regression anchor
            ("forge:opencode:glm", "forge", "opencode", "openrouter",
             "z-ai/glm-4.6", "per_token", 1, "cli-smoke", "ok"),
        ],
    )
    # quality floors low enough that warm-start priors clear them.
    con.execute("CREATE TABLE task_classes (class_id TEXT PRIMARY KEY, default_quality_floor REAL)")
    con.executemany(
        "INSERT INTO task_classes (class_id, default_quality_floor) VALUES (?,?)",
        [("documentation", 0.5), ("analysis", 0.5), ("docs_explainer", 0.5),
         ("code_implementation", 0.5)],
    )
    # tables the selection/cost path reads; empty is fine.
    con.execute("CREATE TABLE model_prices (model_id TEXT PRIMARY KEY, "
                "input_per_1m_usd REAL, output_per_1m_usd REAL, request_overhead_usd REAL)")
    con.execute("CREATE TABLE dispatches (model_used TEXT, task_class TEXT, accepted INTEGER)")
    con.commit()
    con.close()


def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_s1f2_")
    os.close(fd)
    build_db(path)
    orig_db = route_select.DB_PATH
    route_select.DB_PATH = path
    try:
        for tc in ("documentation", "analysis", "docs_explainer"):
            plan = route_select.select_route(RouteTask(tag=f"t-{tc}", task_class=tc,
                                                       error_sensitivity="low"))
            print(f"\n[{tc}] -> route={plan.selected_route} model={plan.selected_model} "
                  f"backend={plan.backend} error={plan.error}")
            check(f"{tc}: backend is spine", plan.backend == "spine")
            check(f"{tc}: no error (no no_spine_route_available)", plan.error is None)
            check(f"{tc}: a spine route was selected",
                  bool(plan.selected_route) and plan.selected_route.startswith("spine:litellm:"))
            check(f"{tc}: a real LiteLLM alias selected", plan.selected_model in SPINE_ALIASES)
            check(f"{tc}: unprobed (NULL last_health) route NOT selected",
                  plan.selected_route != "spine:litellm:unprobed")
            check(f"{tc}: failed-probe route NOT selected",
                  plan.selected_route != "spine:litellm:dead")

        # REGRESSION: forge routing unchanged by the gate edit.
        plan = route_select.select_route(RouteTask(tag="t-code", task_class="code_implementation",
                                                   error_sensitivity="high"))
        print(f"\n[code_implementation] -> route={plan.selected_route} backend={plan.backend} "
              f"error={plan.error}")
        check("code_implementation: backend is forge", plan.backend == "forge")
        check("code_implementation: a forge route selected",
              bool(plan.selected_route) and plan.selected_route.startswith("forge:"))
        check("code_implementation: no error", plan.error is None)
    finally:
        route_select.DB_PATH = orig_db
        os.unlink(path)

    print()
    if not PASS:
        print("SMOKE ROUTE-V2-SPINE: FAIL")
        sys.exit(1)
    print("SMOKE ROUTE-V2-SPINE: ALL PASS")


if __name__ == "__main__":
    main()
