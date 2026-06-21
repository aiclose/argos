"""U2-001: deterministic task_class fallback + one-time backfill.

The dispatches table is pure structured metadata -- there is NO prompt/brief/text
column, so at record time there is nothing to run a real semantic classifier on.
This module is therefore deliberately conservative:

  1. A valid existing task_class is always kept.
  2. Otherwise we do a DETERMINISTIC, explicit tag-prefix -> class_id mapping read
     from the dispatch_id (which usually encodes the tag, e.g. "SMOKE-...").
  3. Anything we cannot justify becomes the explicit "unclassified" SENTINEL
     (never NULL). The sentinel is registered in task_classes so the FK
     dispatches.task_class -> task_classes.class_id stays valid, AND unknown rows
     stay out of the real classes' quality stats (we do NOT dump them into
     "conversation").

This is the SINGLE SOURCE OF TRUTH for the logic: router.py imports
classify_task_class / _ensure_sentinel_task_class from here for the live
/dispatch-record path, and backfill_null_task_class() applies the identical logic
to existing rows. Keeping it in one module guarantees the live path and the
backfill can never drift.

CLI:  python3 backfill_task_class.py     # backfills router.ARGOS_DB, idempotent
"""
import sqlite3
import sys
import time


# The 24 valid class_ids in task_classes (operator-verified).
VALID_TASK_CLASSES = {
    "analysis", "architecture_design", "classification", "code_algorithmic",
    "code_boilerplate", "code_generation", "code_implementation", "conversation",
    "creative", "data_engineering", "debugging", "debugging_intermittent",
    "debugging_simple", "devops", "docs_api", "docs_explainer", "documentation",
    "extraction", "formatting", "refactoring", "security", "test_integration",
    "test_unit", "testing",
}

# Real sentinel value (NOT NULL) for rows with no classifiable signal. Registered
# in task_classes by _ensure_sentinel_task_class so the FK stays valid.
SENTINEL_TASK_CLASS = "unclassified"

# Explicit, conservative tag-prefix -> class_id map. Keys are matched
# case-insensitively against the leading token of dispatch_id (the part before the
# first '-', trailing digits stripped). Only prefixes that map unambiguously to a
# real class are listed; everything else falls through to the sentinel rather than
# being guessed at.
_TAG_CLASS_PREFIXES = {
    "SMOKE":    "testing",
    "TEST":     "testing",
    "GATE":     "testing",
    "DEBUG":    "debugging",
    "BUG":      "debugging",
    "FIX":      "debugging",
    "REFACTOR": "refactoring",
    "DOC":      "documentation",
    "DOCS":     "documentation",
    "SEC":      "security",
    "AUDIT":    "security",
}


def _tag_prefix(dispatch_id):
    """Leading tag token of a dispatch_id, upper-cased, trailing digits stripped.
    e.g. "SMOKE-123" -> "SMOKE", "TEST001" -> "TEST", "" / None -> ""."""
    if not dispatch_id:
        return ""
    head = str(dispatch_id).strip().split("-", 1)[0]
    return head.rstrip("0123456789").upper()


def classify_task_class(dispatch_id, existing):
    """Return a non-empty, FK-valid class_id for a dispatch.

    - keeps a valid existing class untouched,
    - else deterministic tag-prefix mapping,
    - else SENTINEL_TASK_CLASS.
    Never returns None/'' so the caller can never write a NULL task_class.
    """
    if existing and str(existing).strip() in VALID_TASK_CLASSES:
        return str(existing).strip()
    mapped = _TAG_CLASS_PREFIXES.get(_tag_prefix(dispatch_id))
    if mapped:
        return mapped
    return SENTINEL_TASK_CLASS


def _ensure_sentinel_task_class(db):
    """Idempotently register the 'unclassified' sentinel in task_classes so the FK
    dispatches.task_class -> task_classes.class_id stays valid. Neutral
    floor/error_sensitivity are copied from a low-stakes existing row
    ('conversation'); if that row is absent, fall back to sane neutral defaults."""
    row = db.execute(
        "SELECT default_quality_floor, default_error_sensitivity "
        "FROM task_classes WHERE class_id='conversation'"
    ).fetchone()
    floor = row[0] if row else 0.70
    sens = row[1] if row else "low"
    db.execute(
        "INSERT OR IGNORE INTO task_classes "
        "(class_id, parent_class_id, description, default_quality_floor, default_error_sensitivity) "
        "VALUES (?, NULL, ?, ?, ?)",
        (SENTINEL_TASK_CLASS,
         "No classifiable signal at record time (sentinel)",
         floor, sens),
    )


def backfill_null_task_class(db_path=None):
    """One-time, idempotent backfill: set task_class for every dispatches row
    WHERE task_class IS NULL OR task_class='' using the SAME logic as the live
    /dispatch-record path. Safe to re-run. Returns (updated, remaining_null)."""
    if db_path is None:
        import router  # lazy: avoids a circular import (router imports this module)
        db_path = router.ARGOS_DB
    updated = 0
    with sqlite3.connect(db_path, timeout=30) as db:
        # FK target must exist BEFORE we point any row at the sentinel.
        _ensure_sentinel_task_class(db)
        db.commit()
        rows = db.execute(
            "SELECT dispatch_id, task_class FROM dispatches "
            "WHERE task_class IS NULL OR task_class=''"
        ).fetchall()
        for dispatch_id, existing in rows:
            cls = classify_task_class(dispatch_id, existing)
            db.execute(
                "UPDATE dispatches SET task_class=? "
                "WHERE dispatch_id=? AND (task_class IS NULL OR task_class='')",
                (cls, dispatch_id),
            )
            updated += 1
        db.commit()
        remaining = db.execute(
            "SELECT COUNT(*) FROM dispatches WHERE task_class IS NULL OR task_class=''"
        ).fetchone()[0]
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
          f"backfill_null_task_class: updated {updated} rows, "
          f"{remaining} NULL/empty remaining (db={db_path})", flush=True)
    return updated, remaining


if __name__ == "__main__":
    upd, rem = backfill_null_task_class()
    print(f"backfill complete: updated={upd} remaining_null={rem}")
    sys.exit(0 if rem == 0 else 1)
