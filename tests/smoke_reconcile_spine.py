#!/usr/bin/env python3
"""Offline smoke test for ARGOS-S1 FIX 3: spine-aware route reconciliation.

Runnable: `python3 tests/smoke_reconcile_spine.py`. Builds a temp sqlite db (no
network, never touches the real argos.db) and exercises
pricing_puller.reconcile_routes() + reconcile_spine_routes() directly.

The bug this guards against: reconcile_routes auto-disabled ANY enabled route
whose model_id was not a live OpenRouter slug. The 19 spine:litellm routes carry
a LiteLLM ALIAS as model_id (claude-haiku, deepseek-v3, ...), which is never an
OpenRouter slug, so the next pricing_puller run WOULD have wrongly disabled all 19.

Cases:
  1. spine:litellm routes (alias model_id NOT in seen_ids / model_prices) are
     NOT disabled -- the fix.
  2. a genuinely-dead forge :free route (slug absent from OpenRouter) IS STILL
     disabled in the SAME run -- forge behaviour byte-identical.
  3. a live forge route stays enabled.
  4. reconcile_spine_routes() reports the spine routes and disables NOTHING.
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pricing_puller as P  # noqa: E402

failures = []
NOTE_FRAG = "auto-disabled"

# The spine aliases (a representative slice of the 19) -- none are OpenRouter slugs.
SPINE_ALIASES = ["claude-haiku", "deepseek-v3", "or-auto", "zdr-gpt-oss-120b",
                 "kimi-k2.5", "zdr-deepseek-v4-flash"]


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


def build_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_s1f3_")
    os.close(fd)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE model_prices (model_id TEXT PRIMARY KEY, deprecated INTEGER DEFAULT 0)")
    db.execute("""CREATE TABLE routes (
        route_id TEXT PRIMARY KEY, backend TEXT, tool TEXT, access_path TEXT,
        model_id TEXT, enabled INTEGER, notes TEXT, updated_at TEXT, last_health TEXT)""")

    # Only the live forge model is in the catalogue / this fetch.
    db.execute("INSERT INTO model_prices (model_id, deprecated) VALUES ('vendor/live-forge', 0)")

    rows = [
        # 19-ish spine:litellm routes; alias model_id, not OpenRouter slugs.
        *[(f"spine:litellm:{a}", "spine", "litellm", "litellm", a, 1, "spine route", "old",
           ("ok" if a not in ("kimi-k2.5", "zdr-deepseek-v4-flash") else "fail:probe"))
          for a in SPINE_ALIASES],
        # genuinely dead forge :free route (slug absent from OpenRouter) -> must disable
        ("forge:opencode:dead-free", "forge", "opencode", "openrouter",
         "vendor/dead-model:free", 1, "forge free route", "old", "ok"),
        # live forge route -> stays enabled
        ("forge:opencode:live", "forge", "opencode", "openrouter",
         "vendor/live-forge", 1, "forge live route", "old", "ok"),
    ]
    db.executemany(
        "INSERT INTO routes (route_id, backend, tool, access_path, model_id, enabled, "
        "notes, updated_at, last_health) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return db, path


def enabled_of(db, rid):
    return db.execute("SELECT enabled FROM routes WHERE route_id=?", (rid,)).fetchone()["enabled"]


def main():
    db, path = build_db()
    try:
        # The fetch returned only the live forge model; NONE of the spine aliases.
        seen_ids = {"vendor/live-forge"}

        disabled = P.reconcile_routes(db, seen_ids)
        db.commit()
        disabled_ids = {rid for rid, _ in disabled}
        print(f"reconcile_routes disabled: {sorted(disabled_ids)}")

        # Case 1: NO spine route disabled (the bug fix).
        for a in SPINE_ALIASES:
            rid = f"spine:litellm:{a}"
            check(f"spine route {a} NOT disabled", enabled_of(db, rid) == 1)
            check(f"spine route {a} not in disabled list", rid not in disabled_ids)

        # Case 2: dead forge :free route STILL disabled in the same run.
        check("dead forge :free route disabled", enabled_of(db, "forge:opencode:dead-free") == 0)
        check("dead forge :free route in disabled list",
              "forge:opencode:dead-free" in disabled_ids)
        check("dead forge :free route got a note",
              NOTE_FRAG in (db.execute(
                  "SELECT notes FROM routes WHERE route_id='forge:opencode:dead-free'"
              ).fetchone()["notes"] or ""))

        # Case 3: live forge route untouched.
        check("live forge route stays enabled", enabled_of(db, "forge:opencode:live") == 1)

        # Case 4: reconcile_spine_routes reports spine routes and disables NOTHING.
        enabled_before = {f"spine:litellm:{a}": enabled_of(db, f"spine:litellm:{a}")
                          for a in SPINE_ALIASES}
        spine_report = P.reconcile_spine_routes(db)
        db.commit()
        report_ids = {rid for rid, _, _ in spine_report}
        check("spine report covers all spine routes",
              report_ids == {f"spine:litellm:{a}" for a in SPINE_ALIASES})
        check("spine report excludes forge routes",
              not any(rid.startswith("forge:") for rid in report_ids))
        enabled_after = {f"spine:litellm:{a}": enabled_of(db, f"spine:litellm:{a}")
                         for a in SPINE_ALIASES}
        check("reconcile_spine_routes disables NOTHING", enabled_after == enabled_before)
        check("spine report carries last_health",
              any(h == "ok" for _, _, h in spine_report) and
              any(h == "fail:probe" for _, _, h in spine_report))
    finally:
        db.close()
        os.unlink(path)

    print()
    if failures:
        print(f"SMOKE RECONCILE-SPINE: FAIL ({len(failures)} failed)")
        sys.exit(1)
    print("SMOKE RECONCILE-SPINE: ALL PASS")


if __name__ == "__main__":
    main()
