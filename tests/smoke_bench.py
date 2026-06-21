"""Smoke test for BENCH-001: benchmark tasks folded into eval_tasks.json.

Runs OFFLINE -- operates on the WRITTEN eval_tasks.json, no network.

Asserts:
  - every task (old AND new) has the required keys
  - all ids are unique
  - every new benchmark task's category is in the valid taxonomy list
  - every rubric parses (space-separated crit:weight OR JSON dict) and weights ~1.0
  - benchmark tasks have non-empty expected answers
Prints counts per category; ends with "SMOKE BENCH: ALL PASS" or "SMOKE BENCH: FAIL".

Run with: python3 tests/smoke_bench.py
"""

import os
import sys
import json
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_TASKS_PATH = os.path.join(os.path.dirname(HERE), "eval_tasks.json")

REQUIRED_KEYS = ("id", "category", "prompt", "expected", "rubric")

# The task-class taxonomy the benchmark tasks are allowed to map onto.
VALID_TAXONOMY = {
    "reasoning", "analysis", "code_generation", "code_implementation",
    "classification", "extraction",
}

BENCH_PREFIXES = ("mmlu-pro/", "mmlu/", "humaneval/")

_failures = []


def check(cond, msg):
    if not cond:
        _failures.append(msg)
        print(f"  [FAIL] {msg}")
    return cond


def parse_rubric(rubric):
    """Return dict crit->weight, parsing either a JSON dict or
    'crit:weight crit:weight ...' space-separated form. Raises on malformed."""
    rubric = rubric.strip()
    if rubric.startswith("{"):
        d = json.loads(rubric)
        return {k: float(v) for k, v in d.items()}
    out = {}
    for tok in rubric.split():
        crit, _, weight = tok.rpartition(":")
        if not crit:
            raise ValueError(f"rubric token missing 'crit:weight': {tok!r}")
        out[crit] = float(weight)
    if not out:
        raise ValueError("empty rubric")
    return out


def main():
    with open(EVAL_TASKS_PATH, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    check(isinstance(tasks, list), "eval_tasks.json is a JSON array")
    print(f"Loaded {len(tasks)} tasks from eval_tasks.json")

    # 1) required keys present on every task (old AND new). Presence only --
    # some existing live-traffic tasks legitimately carry expected=null.
    for i, t in enumerate(tasks):
        for k in REQUIRED_KEYS:
            check(k in t, f"task[{i}] id={t.get('id','?')!r} missing key {k!r}")

    # 2) unique ids
    ids = [t["id"] for t in tasks]
    dupes = [x for x, c in collections.Counter(ids).items() if c > 1]
    check(not dupes, f"duplicate ids: {dupes}")

    # Identify benchmark tasks.
    bench = [t for t in tasks if t["id"].startswith(BENCH_PREFIXES)]
    check(len(bench) > 0, "found at least one benchmark task")
    print(f"Benchmark tasks: {len(bench)}")

    # 3) new benchmark task categories within valid taxonomy
    for t in bench:
        check(t["category"] in VALID_TAXONOMY,
              f"bench {t['id']!r} category {t['category']!r} not in taxonomy")

    # 4) every rubric parses and weights sum ~1.0 (check ALL tasks)
    for t in tasks:
        try:
            weights = parse_rubric(t["rubric"])
        except Exception as e:
            check(False, f"task {t['id']!r} rubric unparseable: {e}")
            continue
        s = sum(weights.values())
        check(abs(s - 1.0) < 1e-6,
              f"task {t['id']!r} rubric weights sum to {s:.4f} (!= 1.0): {t['rubric']!r}")

    # 5) benchmark tasks have non-empty expected answers
    for t in bench:
        check(isinstance(t["expected"], str) and t["expected"].strip() != "",
              f"bench {t['id']!r} has empty expected answer")

    # Counts per category.
    print("\nCounts per category (all tasks):")
    for cat, n in sorted(collections.Counter(t["category"] for t in tasks).items()):
        print(f"  {cat:18s}: {n}")
    print("\nCounts per category (benchmark tasks only):")
    for cat, n in sorted(collections.Counter(t["category"] for t in bench).items()):
        print(f"  {cat:18s}: {n}")
    print("\nBenchmark tasks by source:")
    src = collections.Counter(t["id"].split("/")[0] for t in bench)
    for s, n in sorted(src.items()):
        print(f"  {s:18s}: {n}")

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed.")
        print("SMOKE BENCH: FAIL")
        return 1
    print("SMOKE BENCH: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
