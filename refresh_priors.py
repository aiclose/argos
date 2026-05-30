"""Argos weekly prior-refresh.

Boosts route success priors with fresh external benchmark data on a schedule.
Benchmark scores drift (new model versions, new leaderboard entries), so the
warm-start seeds in route_priors.py should be refreshed periodically rather than
frozen at first-build values.

Design: this reads a benchmark-snapshot JSON (`benchmark_priors.json`) that holds
the latest known coding-benchmark scores per model, and regenerates the
BENCH_PRIOR table in route_priors.py from it. The snapshot is updated by Claude
during a research pass (see the prior-seeding skill) - this script is the
mechanical merge, kept separate from the (judgement-heavy) data gathering.

Run weekly (cron). It only updates SEED priors; it never touches observed-rate
data, which always overrides seeds in the router.
"""
import json, os, datetime, re

ARGOS = "/home/andy/argos"
SNAPSHOT = f"{ARGOS}/benchmark_priors.json"
PRIORS_PY = f"{ARGOS}/route_priors.py"
LOG = "/home/andy/logs/argos-prior-refresh.log"

def log(m):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {m}"
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "a").write(line + "\n")

def main():
    if not os.path.exists(SNAPSHOT):
        log(f"no snapshot at {SNAPSHOT}; nothing to refresh. (Run a research pass to create it.)")
        return
    snap = json.load(open(SNAPSHOT))
    bench = snap.get("bench_prior", {})
    if not bench:
        log("snapshot has no bench_prior entries; skipping.")
        return
    # regenerate the BENCH_PRIOR dict literal in route_priors.py
    src = open(PRIORS_PY).read()
    # build the new dict body
    lines = []
    for mid, score in bench.items():
        lines.append(f'    {json.dumps(mid)}: {round(float(score),3)},')
    new_block = "BENCH_PRIOR = {\n" + "\n".join(lines) + "\n}"
    # replace the existing BENCH_PRIOR = { ... } block (greedy to first lone "}")
    pattern = re.compile(r"BENCH_PRIOR = \{.*?\n\}", re.S)
    if not pattern.search(src):
        log("could not locate BENCH_PRIOR block in route_priors.py; aborting (no change).")
        return
    new_src = pattern.sub(new_block, src, count=1)
    if new_src == src:
        log("BENCH_PRIOR unchanged.")
        return
    # backup + write
    bak = PRIORS_PY + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    open(bak, "w").write(src)
    open(PRIORS_PY, "w").write(new_src)
    # sanity: must still import
    import py_compile
    try:
        py_compile.compile(PRIORS_PY, doraise=True)
        log(f"refreshed BENCH_PRIOR with {len(bench)} models from snapshot dated {snap.get('as_of','?')}. backup: {os.path.basename(bak)}")
    except Exception as e:
        # revert on failure
        open(PRIORS_PY, "w").write(src)
        log(f"FAILED compile after refresh ({e}); reverted.")

if __name__ == "__main__":
    main()
