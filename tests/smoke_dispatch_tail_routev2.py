#!/usr/bin/env python3
"""STATIC smoke for ARGOS-S2 FIX 2a: dispatch_tail talks to the REAL /route-v2.

Runnable: `python3 tests/smoke_dispatch_tail_routev2.py`. This is a STATIC check -
it reads dispatch_tail.py as source and asserts the wiring. It deliberately does
NOT run dispatch_tail (which needs the UM780 cost_log over scp and a live router).

Guards:
  * ARGOS_URL points at /route-v2, not the dead bare /route endpoint.
  * The response mapping reads the REAL /route-v2 fields.

Mapping note: the ARGOS-S2 brief named selected_litellm_alias / predicted_cost_usd,
but those are fields of the legacy /route RoutingDecision, NOT /route-v2. The live
/route-v2 (route_select.select_route) returns selected_model / selected_route /
effective_cost_usd / predicted_success / quality_floor / rationale / fallback_chain.
dispatch_tail correctly maps THOSE, so this smoke verifies the real fields.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(ROOT, "dispatch_tail.py")

PASS = True


def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False


def main():
    with open(SRC_PATH) as f:
        src = f.read()

    # --- URL: /route-v2 present, dead bare /route absent ---
    check("ARGOS_URL targets /route-v2",
          'ARGOS_URL = "http://127.0.0.1:3020/route-v2"' in src)
    check("dead bare /route endpoint URL is gone",
          '"http://127.0.0.1:3020/route"' not in src)

    # --- response mapping references the REAL /route-v2 fields ---
    check("maps selected_model", 'selected_model' in src)
    check("maps selected_route (alias-bearing fallback)", 'selected_route' in src)
    check("maps effective_cost_usd (the /route-v2 cost field)", 'effective_cost_usd' in src)
    check("maps predicted_success", 'predicted_success' in src)
    check("maps rationale", 'rationale' in src)
    check("maps fallback_chain", 'fallback_chain' in src)

    # --- it actually writes a prediction linked to the dispatch_id ---
    check("inserts into predictions table", 'INSERT INTO predictions' in src)
    check("predictor_version is route-v2-cost-optimised",
          'route-v2-cost-optimised' in src)
    check("no-route case is handled (-noroute suffix)", '-noroute' in src)

    # --- the dead-field assumptions from the brief must NOT have crept in as live
    #     reads of a nonexistent /route-v2 field ---
    check("does not read predicted_cost_usd (a /route field, absent on /route-v2)",
          'predicted_cost_usd' not in src)

    print()
    if not PASS:
        print("SMOKE DISPATCH-TAIL-ROUTEV2: FAIL")
        sys.exit(1)
    print("SMOKE DISPATCH-TAIL-ROUTEV2: ALL PASS")


if __name__ == "__main__":
    main()
