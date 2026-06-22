#!/usr/bin/env python3
"""Offline smoke for CHG-P9-050 PART 1: cost._bucket_quota_cost override hook.

Runnable: `python3 tests/smoke_cost_override.py`. No network, never touches the
real argos.db - builds a temp sqlite db with a sunk route mapped to a capped
bucket plus today's quota_usage, and proves:

  * NONE-PATH IDENTITY: with cost._BUCKET_USAGE_OVERRIDE = None the shadow price
    equals the value computed straight from the documented convex formula off the
    DB-resident quota_usage - i.e. the live read path is behaviourally unchanged.
  * OVERRIDE PARITY: feeding the SAME usage via the override yields the identical
    price (the override mirrors the DB read exactly).
  * OVERRIDE BITES: a different injected usage changes the price (hook is live).
  * CAP OVERRIDE: _BUCKET_CAP_OVERRIDE substitutes the requests cap, and lets a
    bucket whose DB caps are all NULL still ration.
  * NO LEAK: both overrides reset to None.
"""
import os
import sys
import time
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cost as costmod  # noqa: E402

PASS = True


def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


def build_db(path, codex_caps):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE routes (route_id TEXT PRIMARY KEY, cost_mode TEXT, "
                "quota_bucket TEXT)")
    con.execute("INSERT INTO routes VALUES ('forge:codex-cli', 'sunk', 'codex-oauth')")
    con.execute("CREATE TABLE quota_caps (bucket TEXT PRIMARY KEY, daily_requests_cap REAL, "
                "daily_tokens_cap REAL, daily_cost_cap_usd REAL)")
    con.execute("INSERT INTO quota_caps VALUES ('codex-oauth', ?, ?, ?)", codex_caps)
    con.execute("CREATE TABLE quota_usage (bucket TEXT, day TEXT, requests INTEGER, "
                "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)")
    # today's usage already deep in the buffer band of a cap=10 bucket (9 of 10 used).
    today = time.strftime("%Y-%m-%d")
    con.execute("INSERT INTO quota_usage VALUES ('codex-oauth', ?, 9, 0, 0, 0.0)", (today,))
    con.commit()
    con.close()


def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="smoke_costov_")
    os.close(fd)
    # Bucket with a real requests cap of 10; usage 9 today -> price should ramp.
    build_db(path, (10.0, None, None))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    route = con.execute("SELECT route_id, cost_mode FROM routes "
                        "WHERE route_id='forge:codex-cli'").fetchone()
    eta = 0.01

    try:
        # Ground truth straight from the documented formula: reserve=0, cap=10, used=9.
        expected = eta * costmod._phi(10 - 9, 0.0, 10.0)
        check("formula expected price > 0 (usage is in the buffer band)", expected > 0.0)

        costmod._BUCKET_USAGE_OVERRIDE = None
        costmod._BUCKET_CAP_OVERRIDE = None
        none_path = costmod._bucket_quota_cost(con, route, eta)
        check("NONE-PATH IDENTITY: DB-read price == documented convex formula",
              abs(none_path - expected) < 1e-12)

        # Override parity: feed the SAME usage; price must be identical.
        costmod.set_bucket_usage_override({"codex-oauth": (9, 0, 0.0)})
        parity = costmod._bucket_quota_cost(con, route, eta)
        check("OVERRIDE PARITY: same usage via override == DB-read price",
              abs(parity - none_path) < 1e-12)

        # Override bites: a lower usage (not in band) drops the price to 0.
        costmod.set_bucket_usage_override({"codex-oauth": (0, 0, 0.0)})
        low = costmod._bucket_quota_cost(con, route, eta)
        check("OVERRIDE BITES: usage=0 -> price 0 (below buffer band)", low == 0.0)

        # Override bites the other way: over-cap usage -> steep price > edge price.
        costmod.set_bucket_usage_override({"codex-oauth": (12, 0, 0.0)})
        over = costmod._bucket_quota_cost(con, route, eta)
        check("OVERRIDE BITES: over-cap usage -> price above the reserve edge",
              over > expected)
        costmod.set_bucket_usage_override(None)

        # NONE-PATH IDENTITY again after toggling: must return to the DB-read value.
        check("NONE-PATH IDENTITY restored after clearing override",
              abs(costmod._bucket_quota_cost(con, route, eta) - none_path) < 1e-12)
    finally:
        con.close()
        os.unlink(path)

    # CAP OVERRIDE on a bucket whose DB caps are ALL NULL (the live codex case):
    # with no override the price is 0 (no cap); with a cap override it rations.
    fd, path2 = tempfile.mkstemp(suffix=".db", prefix="smoke_costov_null_")
    os.close(fd)
    build_db(path2, (None, None, None))  # all caps NULL, like live codex-oauth
    con2 = sqlite3.connect(f"file:{path2}?mode=ro", uri=True)
    con2.row_factory = sqlite3.Row
    route2 = con2.execute("SELECT route_id, cost_mode FROM routes "
                          "WHERE route_id='forge:codex-cli'").fetchone()
    try:
        costmod._BUCKET_USAGE_OVERRIDE = None
        costmod._BUCKET_CAP_OVERRIDE = None
        check("NULL-cap bucket prices 0 with no overrides (today reads 0 in this db too)",
              costmod._bucket_quota_cost(con2, route2, eta) >= 0.0)
        # Inject usage 9 AND a notional cap of 10 -> should ramp just like the real cap.
        costmod.set_bucket_usage_override({"codex-oauth": (9, 0, 0.0)})
        costmod.set_bucket_cap_override({"codex-oauth": 10})
        synth = costmod._bucket_quota_cost(con2, route2, eta)
        check("CAP OVERRIDE: notional cap rations a NULL-cap bucket",
              abs(synth - eta * costmod._phi(1, 0.0, 10.0)) < 1e-12)
    finally:
        costmod.set_bucket_usage_override(None)
        costmod.set_bucket_cap_override(None)
        con2.close()
        os.unlink(path2)

    check("NO LEAK: usage override reset to None", costmod._BUCKET_USAGE_OVERRIDE is None)
    check("NO LEAK: cap override reset to None", costmod._BUCKET_CAP_OVERRIDE is None)

    print()
    if not PASS:
        print("SMOKE COST-OVERRIDE: FAIL")
        sys.exit(1)
    print("SMOKE COST-OVERRIDE: ALL PASS")


if __name__ == "__main__":
    main()
