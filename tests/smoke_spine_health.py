#!/usr/bin/env python3
"""Smoke test for ARGOS-S1 FIX 1: LiteLLM-targeted path-native health for spine routes.

Exercises route_health.litellm_chat() against the LIVE spine:litellm routes read
from argos.db. This is a LIVE check: it must run where LiteLLM (UM780:4000) is
reachable -- on UM780 itself, or garage over the LAN. Run there as:
    python3 tests/smoke_spine_health.py

Asserts (per the operator's verified ground truth, 2026-06-22):
  - >= 15/19 spine aliases answer OK via LiteLLM.
  - The 3 known-bad ZDR free-tier aliases are marked NOT ok:
      zdr-deepseek-v4-flash (OpenRouter model dead / 404),
      kimi-k2.5            (empty 200),
      zdr-llama-3.3-70b    (free-tier throttled / 429-timeout).

When run OFF the LAN (e.g. the OptiPlex dev box) the DB or LiteLLM endpoint is
unreachable; the test SKIPs cleanly (exit 0) rather than failing -- it is a live
DoD check, not an offline unit test. Prints the per-alias table either way.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_health as RH  # noqa: E402

KNOWN_BAD = {"zdr-deepseek-v4-flash", "kimi-k2.5", "zdr-llama-3.3-70b"}
MIN_OK = 15
# A connection-level failure means we cannot reach LiteLLM from here -> SKIP.
UNREACHABLE_MARKERS = ("no-key-or-model", "URLError", "ConnectionError",
                       "ConnectionRefusedError", "timeout", "gaierror")


def _skip(msg):
    print(f"SMOKE SPINE-HEALTH: SKIP ({msg})")
    sys.exit(0)


def main():
    if not os.path.exists(RH.DB):
        _skip(f"argos.db not present at {RH.DB} (run on UM780/garage)")

    con = sqlite3.connect(f"file:{RH.DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        spine = con.execute(
            "SELECT route_id, model_id, tool, access_path, healthcheck_type "
            "FROM routes WHERE backend='spine' AND enabled=1"
        ).fetchall()
    finally:
        con.close()

    if not spine:
        _skip("no enabled spine routes in argos.db")

    results = []
    for r in spine:
        status, ms, sample = RH.litellm_chat(r)
        results.append((r["model_id"], status, ms, sample))

    # Per-alias table.
    print(f"spine:litellm path-native health ({len(results)} routes):")
    for mid, status, ms, sample in sorted(results):
        flag = "OK " if status == "ok" else "XX "
        print(f"  {flag}{mid:28s} {status:24s} {ms:>6}ms  {sample!r}")

    # If every probe failed at the connection layer we are off-LAN -> SKIP.
    if all(any(m in status for m in UNREACHABLE_MARKERS) for _, status, _, _ in results):
        _skip("LiteLLM endpoint unreachable from here (all probes connection-failed)")

    ok = sum(1 for _, s, _, _ in results if s == "ok")
    by_mid = {mid: s for mid, s, _, _ in results}

    failures = []
    if ok < MIN_OK:
        failures.append(f"only {ok}/{len(results)} ok (need >={MIN_OK})")

    for bad in KNOWN_BAD:
        if bad in by_mid and by_mid[bad] == "ok":
            failures.append(f"known-bad {bad} unexpectedly ok")

    print()
    print(f"  ok={ok}/{len(results)}  known-bad-checked={sorted(b for b in KNOWN_BAD if b in by_mid)}")
    if failures:
        print(f"SMOKE SPINE-HEALTH: FAIL ({'; '.join(failures)})")
        sys.exit(1)
    print("SMOKE SPINE-HEALTH: ALL PASS")


if __name__ == "__main__":
    main()
