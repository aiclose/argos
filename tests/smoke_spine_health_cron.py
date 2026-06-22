#!/usr/bin/env python3
"""Smoke test for CHG-P9-048-debt: spine-health cron wrapper + seed script.

STATIC-ONLY validation -- it does NOT execute cron_spine_health.sh (that needs
garage's .litellm-key file and a reachable LiteLLM on UM780, neither present on
the dev clone). It just asserts the artifacts exist and contain the wiring the
operator's ground truth requires. Safe to run anywhere:

    python3 tests/smoke_spine_health_cron.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRON = os.path.join(ROOT, "cron_spine_health.sh")
SEED = os.path.join(ROOT, "seed_spine_routes.py")

_fails = 0


def check(label, ok):
    global _fails
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        _fails += 1


def main():
    # --- cron_spine_health.sh ---
    cron_exists = os.path.isfile(CRON)
    check("cron_spine_health.sh exists", cron_exists)
    check("cron_spine_health.sh is executable (X_OK)",
          cron_exists and os.access(CRON, os.X_OK))

    cron_txt = open(CRON, encoding="utf-8").read() if cron_exists else ""
    check("invokes route_health.py --only=spine:litellm",
          "route_health.py --only=spine:litellm" in cron_txt)
    check("sets LITELLM_BASE_URL to 192.168.4.10:4000",
          "LITELLM_BASE_URL=" in cron_txt and "192.168.4.10:4000" in cron_txt)
    check("reads /home/andy/argos/.litellm-key",
          "/home/andy/argos/.litellm-key" in cron_txt)
    check("cd /home/andy/argos (deployed path)",
          "cd /home/andy/argos" in cron_txt)

    # --- seed_spine_routes.py ---
    seed_exists = os.path.isfile(SEED)
    check("seed_spine_routes.py exists", seed_exists)

    seed_txt = open(SEED, encoding="utf-8").read() if seed_exists else ""
    check("seed mentions CHG-P9-048", "CHG-P9-048" in seed_txt)
    check("seed defines SPINE_MODELS", "SPINE_MODELS" in seed_txt)

    # Count alias tuples: ("<alias>", "<family>", "<note>")
    tuple_re = re.compile(r'^\s*\("[^"]+",\s*"[^"]+",\s*"[^"]*"\),?\s*$')
    n_aliases = sum(1 for ln in seed_txt.splitlines() if tuple_re.match(ln))
    check(f"seed has 19 alias tuples (found {n_aliases})", n_aliases == 19)

    if _fails:
        print(f"SMOKE SPINE-HEALTH-CRON: FAIL ({_fails} assertion(s) failed)")
        sys.exit(1)
    print("SMOKE SPINE-HEALTH-CRON: ALL PASS")


if __name__ == "__main__":
    main()
