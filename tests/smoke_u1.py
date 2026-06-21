"""Smoke test for U1-001: token capture into argos dispatches.

Runs offline (no network, no real argos.db). Exercises:
  - parse_tokens() against a range of notes strings
  - the dispatches INSERT/UPDATE token path against a TEMP sqlite db whose
    dispatches table mirrors the real schema for the columns we touch.

Run with: python3 tests/smoke_u1.py
"""

import os
import sys
import sqlite3
import tempfile

# Import the module under test (parent dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dispatch_tail


def _check(label, got, expected):
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, expected {expected!r}")
    return ok


def test_parse_tokens():
    print("parse_tokens:")
    cases = [
        ("tokens in=123 out=45", (123, 45)),
        ("tokens in=1,234 out=56", (1234, 56)),
        ("no tokens here", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        # tolerant extras: whitespace, surrounding text, casing
        ("foo IN = 7  blah  OUT = 8 bar", (7, 8)),
    ]
    return all(_check(repr(notes), dispatch_tail.parse_tokens(notes), exp) for notes, exp in cases)


def _make_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_u1_")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE dispatches (
            dispatch_id TEXT PRIMARY KEY,
            ts TEXT,
            source TEXT,
            provider_mode TEXT,
            model_used TEXT,
            task_class TEXT,
            actual_cost_usd REAL,
            status TEXT,
            actual_input_tokens INTEGER,
            actual_output_tokens INTEGER
        )
    """)
    db.commit()
    return db, path


def test_insert_and_update():
    """Mirror the real INSERT + IntegrityError->UPDATE token path on a temp db."""
    print("insert/update token path:")
    db, path = _make_temp_db()
    ok = True
    try:
        # 1. Fresh insert carries parsed tokens.
        tin, tout = dispatch_tail.parse_tokens("tokens in=50 out=20")
        db.execute("""
            INSERT INTO dispatches
            (dispatch_id, ts, source, provider_mode, model_used, task_class,
             actual_cost_usd, status, actual_input_tokens, actual_output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("costlog-1", "2026-06-21", "cost_log_tail", None, "m", "conversation",
              0.01, "ok", tin, tout))
        db.commit()
        row = db.execute("SELECT actual_input_tokens, actual_output_tokens FROM dispatches WHERE dispatch_id='costlog-1'").fetchone()
        ok &= _check("fresh insert tokens", tuple(row), (50, 20))

        # 2. Row that pre-exists with NULL tokens gets backfilled by the UPDATE branch.
        db.execute("""
            INSERT INTO dispatches (dispatch_id, ts, source, task_class, actual_input_tokens, actual_output_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("costlog-2", "2026-06-21", "cost_log_tail", "conversation", None, None))
        db.commit()
        tin, tout = dispatch_tail.parse_tokens("tokens in=99 out=11")
        db.execute(
            "UPDATE dispatches SET actual_input_tokens = ?, actual_output_tokens = ? "
            "WHERE dispatch_id = ? AND (actual_input_tokens IS NULL OR actual_input_tokens = 0)",
            (tin, tout, "costlog-2"))
        db.commit()
        row = db.execute("SELECT actual_input_tokens, actual_output_tokens FROM dispatches WHERE dispatch_id='costlog-2'").fetchone()
        ok &= _check("update NULL->tokens", tuple(row), (99, 11))

        # 3. UPDATE must NOT clobber already-populated tokens.
        db.execute(
            "UPDATE dispatches SET actual_input_tokens = ?, actual_output_tokens = ? "
            "WHERE dispatch_id = ? AND (actual_input_tokens IS NULL OR actual_input_tokens = 0)",
            (1, 1, "costlog-1"))
        db.commit()
        row = db.execute("SELECT actual_input_tokens, actual_output_tokens FROM dispatches WHERE dispatch_id='costlog-1'").fetchone()
        ok &= _check("no clobber of existing tokens", tuple(row), (50, 20))
    finally:
        db.close()
        os.remove(path)
    return ok


def main():
    results = [
        test_parse_tokens(),
        test_insert_and_update(),
    ]
    if all(results):
        print("SMOKE U1: ALL PASS")
        sys.exit(0)
    else:
        print("SMOKE U1: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
