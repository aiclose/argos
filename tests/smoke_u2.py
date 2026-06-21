"""Smoke test for U2-001: eliminate NULL task_class on the record path.

Runs fully offline (no network, no real argos.db). Exercises:
  - classify_task_class(): valid class kept, known tag prefix mapped, no signal
    -> "unclassified" sentinel (never NULL).
  - backfill_null_task_class() against a TEMP sqlite db seeded with real
    class_ids + some NULL/empty rows: NULL count goes to 0, sentinel row is
    registered in task_classes, and a re-run is a no-op (idempotent).

Imports the real helper module (backfill_task_class) -- it has no heavy deps and
does not import router at module load, so this stays offline-safe.

Run with: python3 tests/smoke_u2.py
"""

import os
import sys
import sqlite3
import tempfile

# Import the module under test (parent dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backfill_task_class as btc


def _check(label, got, expected):
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, expected {expected!r}")
    return ok


# The 24 real class_ids (operator-verified) for seeding the temp task_classes.
REAL_CLASS_IDS = [
    "analysis", "architecture_design", "classification", "code_algorithmic",
    "code_boilerplate", "code_generation", "code_implementation", "conversation",
    "creative", "data_engineering", "debugging", "debugging_intermittent",
    "debugging_simple", "devops", "docs_api", "docs_explainer", "documentation",
    "extraction", "formatting", "refactoring", "security", "test_integration",
    "test_unit", "testing",
]


def test_classify():
    print("classify_task_class:")
    ok = True
    # 1. A valid existing class is kept untouched.
    ok &= _check("keeps valid existing", btc.classify_task_class("SMOKE-1", "code_generation"), "code_generation")
    # 2. Known tag prefixes map deterministically.
    ok &= _check("SMOKE- -> testing", btc.classify_task_class("SMOKE-001", None), "testing")
    ok &= _check("TEST001 -> testing", btc.classify_task_class("TEST001", ""), "testing")
    ok &= _check("FIX- -> debugging", btc.classify_task_class("FIX-42", None), "debugging")
    ok &= _check("DOCS- -> documentation", btc.classify_task_class("DOCS-readme", None), "documentation")
    ok &= _check("AUDIT- -> security", btc.classify_task_class("AUDIT-7", None), "security")
    # 3. Invalid existing + no recognizable prefix -> sentinel (NEVER None/empty).
    sentinel = btc.classify_task_class("CSCAN-99", None)
    ok &= _check("no signal -> sentinel", sentinel, "unclassified")
    ok &= _check("invalid existing -> sentinel", btc.classify_task_class("WHATEVER-1", "bogus_class"), "unclassified")
    ok &= _check("sentinel is non-empty", bool(sentinel), True)
    # 4. Every mapped target must be a real class_id (no FK surprises).
    mapped_targets = set(btc._TAG_CLASS_PREFIXES.values())
    ok &= _check("mapped targets are real classes", mapped_targets <= set(REAL_CLASS_IDS), True)
    return ok


def _make_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_u2_")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE task_classes (
            class_id TEXT PRIMARY KEY,
            parent_class_id TEXT,
            description TEXT,
            default_quality_floor REAL,
            default_error_sensitivity TEXT
        )
    """)
    db.execute("""
        CREATE TABLE dispatches (
            dispatch_id TEXT PRIMARY KEY,
            ts TEXT,
            source TEXT,
            model_used TEXT,
            task_class TEXT REFERENCES task_classes(class_id)
        )
    """)
    for cid in REAL_CLASS_IDS:
        db.execute(
            "INSERT INTO task_classes(class_id, parent_class_id, description, default_quality_floor, default_error_sensitivity) "
            "VALUES (?, NULL, ?, ?, ?)",
            (cid, f"{cid} desc", 0.70, "medium"),
        )
    db.commit()
    return db, path


def test_backfill():
    print("backfill_null_task_class (temp db):")
    db, path = _make_temp_db()
    ok = True
    try:
        # Seed: 2 already-good rows, 3 NULL/empty rows (one mappable, two no-signal).
        seed = [
            ("D-good-1", "code_generation"),   # keep
            ("SMOKE-keep", "testing"),         # keep
            ("SMOKE-null-1", None),            # -> testing (mapped)
            ("CSCAN-null-2", None),            # -> unclassified
            ("RANDOM-empty-3", ""),            # -> unclassified
        ]
        for did, tc in seed:
            db.execute("INSERT INTO dispatches(dispatch_id, ts, source, model_used, task_class) VALUES (?, ?, ?, ?, ?)",
                       (did, "2026-06-21", "test", "m", tc))
        db.commit()

        before = db.execute("SELECT COUNT(*) FROM dispatches WHERE task_class IS NULL OR task_class=''").fetchone()[0]
        ok &= _check("NULL/empty before backfill", before, 3)
        db.close()  # backfill opens its own connection on the same file

        updated, remaining = btc.backfill_null_task_class(db_path=path)
        ok &= _check("rows updated", updated, 3)
        ok &= _check("NULL/empty after backfill", remaining, 0)

        db = sqlite3.connect(path)
        # Sentinel row registered in task_classes (FK target exists).
        sent = db.execute("SELECT COUNT(*) FROM task_classes WHERE class_id='unclassified'").fetchone()[0]
        ok &= _check("sentinel registered in task_classes", sent, 1)
        # Mapped row got the deterministic class; no-signal rows got the sentinel.
        ok &= _check("mapped NULL -> testing", db.execute("SELECT task_class FROM dispatches WHERE dispatch_id='SMOKE-null-1'").fetchone()[0], "testing")
        ok &= _check("no-signal NULL -> unclassified", db.execute("SELECT task_class FROM dispatches WHERE dispatch_id='CSCAN-null-2'").fetchone()[0], "unclassified")
        ok &= _check("empty -> unclassified", db.execute("SELECT task_class FROM dispatches WHERE dispatch_id='RANDOM-empty-3'").fetchone()[0], "unclassified")
        # Pre-existing good rows untouched.
        ok &= _check("good row untouched", db.execute("SELECT task_class FROM dispatches WHERE dispatch_id='D-good-1'").fetchone()[0], "code_generation")
        # Every dispatch references a real task_classes row (FK integrity).
        orphans = db.execute(
            "SELECT COUNT(*) FROM dispatches d LEFT JOIN task_classes t ON d.task_class=t.class_id WHERE t.class_id IS NULL"
        ).fetchone()[0]
        ok &= _check("no FK orphans", orphans, 0)
        db.close()

        # Idempotent: a second run changes nothing.
        upd2, rem2 = btc.backfill_null_task_class(db_path=path)
        ok &= _check("re-run updates 0", upd2, 0)
        ok &= _check("re-run still 0 NULL", rem2, 0)
    finally:
        try:
            db.close()
        except Exception:
            pass
        os.remove(path)
    return ok


def main():
    results = [
        test_classify(),
        test_backfill(),
    ]
    if all(results):
        print("SMOKE U2: ALL PASS")
        sys.exit(0)
    else:
        print("SMOKE U2: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
