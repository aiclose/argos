#!/usr/bin/env python3
"""Offline smoke test for U7-001: routes-table reconciliation in pricing_puller.

Runs fully OFFLINE against a temp sqlite db -- NO network, no real OpenRouter
fetch. Exercises pricing_puller.reconcile_routes(db, seen_ids) directly.

Cases covered:
  1. route -> LIVE model (in model_prices, deprecated=0, in seen_ids) stays enabled
  2. route -> DEPRECATED model (model_prices.deprecated=1)        -> disabled + note
  3. route -> ABSENT model (not in model_prices, not in seen_ids) -> disabled + note
  4. route -> dead model but ALREADY disabled -> stays disabled, NO duplicate note
  5. route with model_id IS NULL (non-OpenRouter backend)         -> never touched
  6. idempotency: a second reconcile run disables 0 new routes, no new notes
  7. no route is ever re-enabled (0 -> 1)

Prints PASS/FAIL per case; ends with "SMOKE U7: ALL PASS" or "SMOKE U7: FAIL".

Run with: python3 tests/smoke_u7.py
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pricing_puller as P  # noqa: E402

NOTE_FRAG = "auto-disabled"

failures = []

def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        failures.append(name)


def build_db():
    """Temp db with routes + model_prices, seeded for all cases."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_u7_")
    os.close(fd)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row

    # Minimal schema (only columns reconcile_routes touches/reads).
    db.execute("""
        CREATE TABLE model_prices (
            model_id TEXT PRIMARY KEY,
            deprecated INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE routes (
            route_id TEXT PRIMARY KEY,
            model_id TEXT,
            enabled INTEGER,
            notes TEXT,
            updated_at TEXT
        )
    """)

    # model_prices catalogue
    db.executemany(
        "INSERT INTO model_prices (model_id, deprecated) VALUES (?, ?)",
        [
            ("vendor/live-model", 0),       # live
            ("vendor/dep-model", 1),        # deprecated in catalogue
            # absent-model intentionally NOT inserted
        ],
    )

    # routes
    db.executemany(
        "INSERT INTO routes (route_id, model_id, enabled, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
        [
            ("r:live",      "vendor/live-model",   1, "live route",       "old"),
            ("r:deprecated","vendor/dep-model",    1, None,               "old"),
            ("r:absent",    "vendor/absent-model", 1, "existing note",    "old"),
            ("r:already",   "vendor/absent-model", 0, " [auto-disabled 2026-01-01: model deprecated/absent on OpenRouter]", "old"),
            ("r:nullmodel", None,                  1, "non-openrouter",   "old"),
        ],
    )
    db.commit()
    return db, path


def notes_of(db, rid):
    row = db.execute("SELECT notes FROM routes WHERE route_id = ?", (rid,)).fetchone()
    return row["notes"] or ""


def enabled_of(db, rid):
    return db.execute("SELECT enabled FROM routes WHERE route_id = ?", (rid,)).fetchone()["enabled"]


def main():
    db, path = build_db()
    try:
        # seen_ids: only the live model was returned this fetch.
        seen_ids = {"vendor/live-model"}

        # Snapshot the already-disabled note to detect duplication later.
        already_note_before = notes_of(db, "r:already")

        disabled = P.reconcile_routes(db, seen_ids)
        db.commit()
        disabled_ids = {rid for rid, _ in disabled}

        print("Run 1:")
        # Case 1: live route untouched
        check("live route stays enabled", enabled_of(db, "r:live") == 1)
        check("live route note unchanged", notes_of(db, "r:live") == "live route")
        check("live route not in disabled list", "r:live" not in disabled_ids)

        # Case 2: deprecated model -> disabled + note appended
        check("deprecated route disabled", enabled_of(db, "r:deprecated") == 0)
        check("deprecated route got note", NOTE_FRAG in notes_of(db, "r:deprecated"))
        check("deprecated route in disabled list", "r:deprecated" in disabled_ids)

        # Case 3: absent model -> disabled, existing note preserved (appended)
        check("absent route disabled", enabled_of(db, "r:absent") == 0)
        check("absent route note appended (existing kept)",
              notes_of(db, "r:absent").startswith("existing note") and NOTE_FRAG in notes_of(db, "r:absent"))
        check("absent route in disabled list", "r:absent" in disabled_ids)

        # Case 4: already-disabled dead route -> untouched, NO duplicate note
        check("already-disabled route stays disabled", enabled_of(db, "r:already") == 0)
        check("already-disabled route note NOT duplicated",
              notes_of(db, "r:already") == already_note_before)
        check("already-disabled route not in disabled list", "r:already" not in disabled_ids)
        check("already-disabled note has exactly one auto-disabled tag",
              notes_of(db, "r:already").count(NOTE_FRAG) == 1)

        # Case 5: model_id IS NULL -> never touched
        check("null-model route stays enabled", enabled_of(db, "r:nullmodel") == 1)
        check("null-model route not in disabled list", "r:nullmodel" not in disabled_ids)

        # Disabled-list shape
        check("exactly 2 routes disabled this run", len(disabled) == 2)

        # --- Run 2: idempotency ------------------------------------------------
        print("Run 2 (idempotency):")
        notes_before = {rid: notes_of(db, rid) for rid in
                        ("r:live", "r:deprecated", "r:absent", "r:already", "r:nullmodel")}
        enabled_before = {rid: enabled_of(db, rid) for rid in notes_before}

        disabled2 = P.reconcile_routes(db, seen_ids)
        db.commit()

        check("second run disables 0 new routes", len(disabled2) == 0)
        notes_after = {rid: notes_of(db, rid) for rid in notes_before}
        enabled_after = {rid: enabled_of(db, rid) for rid in notes_before}
        check("second run leaves all notes unchanged", notes_after == notes_before)
        check("second run leaves all enabled flags unchanged", enabled_after == enabled_before)

        # No auto-disabled tag ever appears more than once on any route.
        check("no route has a duplicated auto-disabled tag",
              all(notes_of(db, rid).count(NOTE_FRAG) <= 1 for rid in notes_before))

        # --- Reactivation guard: nothing ever goes 0 -> 1 ----------------------
        print("Reactivation guard:")
        # Even if the model "reappears", reconcile must never re-enable.
        seen_ids_revived = {"vendor/live-model", "vendor/absent-model"}
        db.execute("UPDATE model_prices SET deprecated = 0 WHERE model_id = 'vendor/dep-model'")
        # also pretend absent-model came back into the catalogue
        db.execute("INSERT OR REPLACE INTO model_prices (model_id, deprecated) VALUES ('vendor/absent-model', 0)")
        db.commit()
        disabled3 = P.reconcile_routes(db, seen_ids_revived)
        db.commit()
        check("revived models do not re-enable any route", len(disabled3) == 0)
        check("r:deprecated still disabled after model revived", enabled_of(db, "r:deprecated") == 0)
        check("r:absent still disabled after model revived", enabled_of(db, "r:absent") == 0)

    finally:
        db.close()
        os.unlink(path)

    print()
    if failures:
        print(f"SMOKE U7: FAIL ({len(failures)} failed: {', '.join(failures)})")
        sys.exit(1)
    print("SMOKE U7: ALL PASS")


if __name__ == "__main__":
    main()
