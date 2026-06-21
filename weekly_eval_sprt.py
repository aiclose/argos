from __future__ import annotations
import os as _os
_envf = "/home/andy/argos/.env"
if _os.path.exists(_envf):
    for _l in open(_envf):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip().strip(chr(34)).strip(chr(39)))

#!/usr/bin/env python3
"""
Argos weekly champion-challenger eval with Wald SPRT promotion gate.
Self-contained: model invocation (OpenRouter) + LLM-judge scoring + SPRT + argos.db writes.

SPRT (per promotion-gate-monte-carlo-2026-05-28): alpha=0.05, beta=0.2,
H1 = champion_rate + 0.08 (cold-start fixed; adaptive H1 is a later enhancement).
Promote when log-LR >= log((1-beta)/alpha)=log(16); reject when <= log(beta/(1-alpha))=log(0.21).
Cap 100 trials/arm.

Usage: python3 weekly_eval_sprt.py [--dry-run] [--db PATH] [--tasks TASKS.json]
                                   [--max-trials N] [--classes c1,c2]
"""
import argparse, json, math, os, sqlite3, time, uuid, urllib.request
from datetime import datetime, timezone

OR_KEY = os.getenv("OPENROUTER_API_KEY")  # env-only, no hardcoded secret
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- LLM-judge config (U5-001) -------------------------------------------------
# Pinned, trivially-swappable judge model. Default is a STRONG, cheap, fast model
# (good for a weekly job). Swap by editing this one constant. Verified-live options:
#   "google/gemini-2.5-flash"  -> default  (strong / cheap / fast)
#   "google/gemini-2.5-pro"    -> higher-rigor option (strongest, slower/pricier)
#   "deepseek/deepseek-v3.2"   -> alt strong
#   "qwen/qwen3-coder-plus"    -> alt strong (code-leaning)
#   "openai/gpt-oss-120b:free" -> OLD default; weak / DRACO-gameable (do NOT use)
#   grok-4 is DEPRECATED -> do NOT use.
#
# DRACO CAVEAT: absolute judge scores shift ~10-25 points depending on which judge
# model you pin. Trust the RELATIVE ranking between champion and challenger, NOT the
# absolute value. Pinning ONE judge model keeps every comparison within a single
# bake-off consistent, so the relative ordering stays meaningful.
JUDGE_MODEL = "google/gemini-2.5-flash"
JUDGE_SAMPLES = 3            # N=3 judge calls per (task, output); per-criterion mean -> weighted final
LATENCY_NEUTRAL_SCORE = 0.7  # latency-only / no-reference tasks: neutral pass (correctness skipped)
PASS_THRESHOLD = 0.7         # judge score >= 0.7 counts as a success (binary outcome for SPRT)

# --- Benchmark-score cache (CACHE-001) ----------------------------------------
# A (model_id, task_id, JUDGE_MODEL) benchmark score is DETERMINISTIC given the
# model version + judge, so re-running every task x N=3 against unchanged models
# every week is wasted OpenRouter cost. cached_run_trial() memoises run_trial()'s
# (success, score, latency) tuple in the bench_cache table.
#
# HONEST LIMITATION (do NOT pretend otherwise): OpenRouter model SLUGS are stable
# strings, but the model BEHIND a slug can change silently -- there is NO reliable
# per-call version/content hash exposed by the API. So invalidation is NOT content-
# based. We invalidate on three honest signals only:
#   1. TTL  -- re-run if the cached score is older than BENCH_CACHE_TTL_DAYS (the
#              best available proxy for "the model behind the slug may have moved").
#   2. force -- --force-rescore ignores the cache and refreshes every entry.
#   3. judge -- JUDGE_MODEL is part of the cache key (U5 showed absolute scores
#              shift by judge), so changing the pinned judge naturally misses the
#              old rows and re-judges.
BENCH_CACHE_TTL_DAYS = 30    # re-run a cached (model,task,judge) score older than this

# SPRT params
ALPHA, BETA = 0.05, 0.10
H1_OFFSET = 0.08
LOG_A = math.log((1 - BETA) / ALPHA)   # ~2.77 (promote boundary)
LOG_B = math.log(BETA / (1 - ALPHA))   # ~-1.56 (reject boundary)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def or_chat(model, prompt, max_tokens=600, timeout=90, temperature=0.2):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(OR_URL, data=body,
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
        m = d["choices"][0]["message"]
        usage = d.get("usage", {})
        return (m.get("content") or m.get("reasoning") or ""), usage

# === LLM-judge (U5-001) =======================================================
# DRACO-shaped weighted per-criterion judge with negative (confident-wrong)
# criteria, N=3 sampling, and a pinned swappable judge model (see JUDGE_MODEL).
import re as _re

def _clamp(x):
    """Coerce to a float in [0,1]; NaN/None/garbage -> 0.0."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, x))

def parse_rubric(rubric):
    """Parse a task rubric into a normalised [(criterion, weight), ...] list.

    Handles BOTH on-disk formats:
      (a) space-separated string  "code-correctness:0.6 docstring:0.15 style:0.1"
      (b) JSON dict               {"code-correctness":0.6, "style":0.15, ...}
          (also a dict handed in as a JSON string).
    Weights are normalised defensively to sum to 1.0 (they are only INTENDED to
    sum ~1.0 on disk). Degenerate input -- empty, unparseable, or total weight
    <= 0 -- falls back to [("correctness", 1.0)].
    """
    pairs = []
    if isinstance(rubric, dict):
        for k, v in rubric.items():
            try:
                pairs.append((str(k).strip(), float(v)))
            except (TypeError, ValueError):
                continue
    elif isinstance(rubric, str):
        s = rubric.strip()
        if s.startswith("{"):                 # tolerate a JSON object passed as a string
            try:
                d = json.loads(s)
                if isinstance(d, dict):
                    return parse_rubric(d)
            except Exception:
                pass
        for tok in s.split():
            if ":" in tok:
                name, _, w = tok.rpartition(":")
                try:
                    pairs.append((name.strip(), float(w)))
                except ValueError:
                    continue
    # keep only named, positive, non-NaN weights
    pairs = [(c, w) for c, w in pairs if c and w > 0 and w == w]
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return [("correctness", 1.0)]
    return [(c, w / total) for c, w in pairs]

def _is_latency_only(criteria):
    """True when every criterion is a latency criterion (no correctness to grade)."""
    return bool(criteria) and all("latency" in c.lower() for c, _ in criteria)

def _judge_prompt(task, output, criteria):
    """Build the DRACO-shaped weighted per-criterion judge prompt."""
    crit_lines = "\n".join(f'  - "{c}" (weight {w:.2f})' for c, w in criteria)
    example = "{" + ", ".join(f'"{c}": 0.0' for c, _ in criteria) + "}"
    return f"""You are a STRICT, SKEPTICAL grader. Grade the ANSWER against each weighted
rubric criterion below, comparing it to the EXPECTED reference answer.
Score EACH criterion from 0.0 (fails completely) to 1.0 (fully satisfies it).

NEGATIVE / CONFIDENT-WRONG RULES (apply strictly -- this is the point of the grade):
  - A confidently INCORRECT answer must score LOWER than an answer that is honestly
    unsure, hedged, or only partially correct. Confident wrongness is the WORST outcome.
  - Penalise HARDEST on the correctness criteria for hallucinated specifics: made-up
    function names, fabricated values, invented APIs, or any claim NOT supported by the
    EXPECTED reference answer.
  - Do NOT reward fluent, authoritative prose that is factually wrong.
  - An answer that admits uncertainty but is partially correct scores HIGHER than a
    polished answer that is wrong.

CRITERIA (each scored 0.0-1.0):
{crit_lines}

TASK:
{str(task.get('prompt',''))[:1500]}

EXPECTED (reference answer):
{str(task.get('expected',''))[:800]}

ANSWER (to grade):
{str(output)[:2500]}

Return STRICT JSON ONLY: an object mapping EACH criterion name above to its score, e.g.
{example}
Output ONLY the JSON object -- no prose, no markdown fences."""

def _extract_json_obj(txt):
    """Tolerant: return the first JSON object found in txt, else None."""
    try:
        v = json.loads(txt)
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    m = _re.search(r"\{.*\}", txt, _re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    return None

def _sample_score(txt, criteria):
    """Score ONE judge response. Returns (weighted_score_float, per_criterion_dict).

    Robustness ladder: strict/tolerant JSON object of per-criterion scores ->
    weighted sum; else fall back to a single number applied to all criteria;
    else 0.0. Never raises.
    """
    obj = _extract_json_obj(txt)
    per = {}
    if obj is not None:
        for c, _ in criteria:
            if c in obj:
                per[c] = _clamp(obj[c])
    if per:
        # partial JSON: fill any unscored criteria with the mean of scored ones
        # (neutral) rather than zeroing them out.
        if len(per) < len(criteria):
            fill = sum(per.values()) / len(per)
            for c, _ in criteria:
                per.setdefault(c, fill)
        score = sum(per[c] * w for c, w in criteria)
        return _clamp(score), per
    # tolerant fallback: a bare number anywhere in the text
    m = _re.search(r"(\d*\.?\d+)", txt or "")
    if m:
        n = _clamp(m.group(1))
        return n, {c: n for c, _ in criteria}
    return 0.0, {c: 0.0 for c, _ in criteria}

def judge_detailed(task, output, timeout=90):
    """DRACO-shaped weighted judge with N=JUDGE_SAMPLES sampling.

    Returns a dict for transparency/logging:
      {"score": float, "per_criterion": {crit: mean_score}, "samples": [final,...],
       "variance": float, "n_samples": int, "note": str (optional)}

    AGGREGATION (documented): each of the N samples is scored per-criterion; we take
    the per-criterion MEAN across samples, THEN the weighted sum -> final "score".
    "variance" is the population variance of the per-sample weighted finals (an
    agreement signal -- low variance == the judge samples agreed).
    """
    criteria = parse_rubric(task.get("rubric"))
    # latency-only / no reference answer -> nothing to grade for correctness.
    if task.get("expected") is None or _is_latency_only(criteria):
        return {"score": LATENCY_NEUTRAL_SCORE,
                "per_criterion": {c: LATENCY_NEUTRAL_SCORE for c, _ in criteria},
                "samples": [], "variance": 0.0, "n_samples": 0,
                "note": "latency-only/no-reference: neutral pass (correctness skipped)"}

    prompt = _judge_prompt(task, output, criteria)
    sample_finals, sample_pers = [], []
    for _ in range(JUDGE_SAMPLES):
        try:
            # temperature ~0.3 so the N samples genuinely vary.
            txt, _u = or_chat(JUDGE_MODEL, prompt, max_tokens=300,
                              timeout=timeout, temperature=0.3)
        except Exception:
            continue
        s, per = _sample_score(txt, criteria)
        sample_finals.append(s)
        sample_pers.append(per)

    if not sample_finals:
        return {"score": 0.0, "per_criterion": {}, "samples": [], "variance": 0.0,
                "n_samples": 0, "note": "no judge samples (all calls failed)"}

    per_mean = {}
    for c, _ in criteria:
        vals = [p[c] for p in sample_pers if c in p]
        per_mean[c] = sum(vals) / len(vals) if vals else 0.0
    final = _clamp(sum(per_mean[c] * w for c, w in criteria))
    mean_final = sum(sample_finals) / len(sample_finals)
    variance = sum((s - mean_final) ** 2 for s in sample_finals) / len(sample_finals)
    return {"score": final, "per_criterion": per_mean, "samples": sample_finals,
            "variance": variance, "n_samples": len(sample_finals)}

def judge(task, output, timeout=90):
    """LLM-judge score 0.0-1.0 against the task rubric (run_trial's contract).

    Backward compatible: returns a single float in [0,1]. Defensive: never raises
    out into run_trial -- any failure collapses to 0.0. Use judge_detailed() for
    the per-criterion breakdown / sample variance.
    """
    try:
        return _clamp(judge_detailed(task, output, timeout=timeout)["score"])
    except Exception:
        return 0.0

def run_trial(model, task):
    """Run one task against one model, return (success_bool, score, latency_ms)."""
    t0 = time.time()
    try:
        out, _ = or_chat(model, task["prompt"], max_tokens=700)
        score = judge(task, out)
        lat = int((time.time() - t0) * 1000)
        return (1 if score >= PASS_THRESHOLD else 0), score, lat
    except Exception as e:
        return 0, 0.0, int((time.time() - t0) * 1000)

# === Benchmark-score cache (CACHE-001) ========================================
def ensure_bench_cache(conn):
    """Idempotently create the bench_cache table. judge_model is part of the PK
    because U5 showed absolute scores shift by judge -- a cached score is only
    valid for the exact judge model that produced it."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bench_cache ("
        " model_id TEXT, task_id TEXT, judge_model TEXT,"
        " success INT, score REAL, latency_ms INT, scored_at TEXT,"
        " PRIMARY KEY (model_id, task_id, judge_model))")
    conn.commit()

def _age_days(scored_at):
    """Age in days of an ISO scored_at timestamp vs now (UTC). Raises on garbage,
    which callers treat as 'not fresh' / fall through to a re-run."""
    dt = datetime.fromisoformat(scored_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

def cached_run_trial(conn, model, task, ttl_days=BENCH_CACHE_TTL_DAYS, force=False):
    """run_trial() with a TTL'd benchmark-score cache keyed on
    (model_id, task_id, JUDGE_MODEL).

    Returns (success:int, score:float, latency_ms:int, cache_state:str) where
    cache_state is one of {"hit","miss","stale"}. The first THREE elements are
    byte-for-byte the same tuple shape run_trial() returns, so SPRT and the
    bake_off_decisions writes are unaffected -- callers just ignore the 4th value.

    HIT  -> fresh cached row (scored within ttl_days), same judge: returned WITHOUT
            calling the model or judge.
    MISS -> no cached row: live run_trial(), then cache the result.
    STALE-> a row existed but is older than ttl_days (or force re-judges it): live
            run_trial(), then refresh the cached row's scored_at to now.

    Best-effort: ANY cache read/write error falls back to a live run_trial() and
    NEVER raises -- the cache is a cost optimisation, not a correctness dependency.
    """
    task_id = task.get("id")
    judge_model = JUDGE_MODEL   # read the module global at call time (judge swaps -> new key)

    # --- read ---------------------------------------------------------------
    row = None
    try:
        if task_id is not None:
            row = conn.execute(
                "SELECT success, score, latency_ms, scored_at FROM bench_cache "
                "WHERE model_id=? AND task_id=? AND judge_model=?",
                (model, task_id, judge_model)).fetchone()
    except Exception:
        row = None  # cache read failed -> treat as a miss, fall through to live run

    if row is not None and not force:
        try:
            if _age_days(row[3]) < ttl_days:
                # HIT: fresh + same judge -> no model/judge calls.
                return int(row[0]), float(row[1]), int(row[2]), "hit"
        except Exception:
            pass  # unparseable timestamp -> treat as stale, re-run below
        state = "stale"
    elif force:
        state = "stale" if row is not None else "miss"
    else:
        state = "miss"

    # --- live run (MISS / STALE / force) ------------------------------------
    success, score, lat = run_trial(model, task)

    # --- write (best-effort UPSERT; PK replace) -----------------------------
    try:
        if task_id is not None:
            conn.execute(
                "INSERT OR REPLACE INTO bench_cache "
                "(model_id, task_id, judge_model, success, score, latency_ms, scored_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (model, task_id, judge_model, int(success), float(score), int(lat), now_iso()))
            conn.commit()
    except Exception:
        pass  # cache write failed -> result is still valid, just not memoised

    return success, score, lat, state

def bench_cache_stats(conn, ttl_days=BENCH_CACHE_TTL_DAYS):
    """(total_entries, stale_entries) currently in bench_cache."""
    total = conn.execute("SELECT COUNT(*) FROM bench_cache").fetchone()[0]
    stale = 0
    for (sa,) in conn.execute("SELECT scored_at FROM bench_cache"):
        try:
            if _age_days(sa) >= ttl_days:
                stale += 1
        except Exception:
            stale += 1  # unparseable -> count as stale (would be re-run)
    return total, stale

def sprt_step(log_lr, x, p0, p1):
    """Update log-likelihood-ratio with one Bernoulli outcome x."""
    if x:
        return log_lr + math.log(p1 / p0)
    return log_lr + math.log((1 - p1) / (1 - p0))

def sprt_decision(log_lr):
    if log_lr >= LOG_A:
        return "promote"
    if log_lr <= LOG_B:
        return "reject"
    return "continue"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default="/home/andy/argos/argos.db")
    ap.add_argument("--tasks", default="/home/andy/argos/eval_tasks.json")
    ap.add_argument("--max-trials", type=int, default=100)
    ap.add_argument("--classes", default="")
    ap.add_argument("--champion", default="deepseek/deepseek-chat")
    ap.add_argument("--challenger", default="openai/gpt-oss-120b:free")
    # CACHE-001: benchmark-score cache controls
    ap.add_argument("--force-rescore", action="store_true",
                    help="ignore the bench_cache and re-run + refresh every (model,task,judge) score")
    ap.add_argument("--cache-ttl-days", type=int, default=BENCH_CACHE_TTL_DAYS,
                    help=f"re-run a cached score older than N days (default {BENCH_CACHE_TTL_DAYS})")
    ap.add_argument("--cache-stats", action="store_true",
                    help="print bench_cache entry/stale counts and exit")
    args = ap.parse_args()

    tasks = json.load(open(args.tasks))
    by_class = {}
    for t in tasks:
        by_class.setdefault(t["category"], []).append(t)

    classes = args.classes.split(",") if args.classes else list(by_class.keys())
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_bench_cache(conn)  # CACHE-001: idempotent table create

    if args.cache_stats:
        total, stale = bench_cache_stats(conn, ttl_days=args.cache_ttl_days)
        print(f"bench_cache: {total} (model,task,judge) entries, {stale} stale "
              f"(>{args.cache_ttl_days}d) -> would re-run on next eval")
        return

    round_id = conn.execute(
        "INSERT INTO bake_off_rounds (task_class, sample_size, status, started_at, created_at) "
        "VALUES (?,?,?,?,?)",
        ("|".join(classes)[:60], args.max_trials, "running", now_iso(), now_iso())
    ).lastrowid
    conn.commit()
    print(f"round {round_id}: champion={args.champion} challenger={args.challenger}")
    print(f"SPRT boundaries: promote>={LOG_A:.3f} reject<={LOG_B:.3f}")

    summary = []
    cache_stats = {"hit": 0, "miss": 0, "stale": 0}  # CACHE-001: round-level tally
    for cls in classes:
        ctasks = by_class.get(cls, [])
        if not ctasks:
            continue
        # Champion success rate p0: use the incumbent's OBSERVED accept rate for this
        # class from dispatches (now that labels exist); fall back to 0.70 only if
        # there is too little history. This is the research's indifference-zone p0=p_c.
        prow = conn.execute(
            "SELECT COUNT(*) n, AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) rate "
            "FROM dispatches WHERE model_used=? AND task_class=? AND accepted IS NOT NULL",
            (args.champion, cls)).fetchone()
        if prow and prow[0] and prow[0] >= 5 and prow[1] is not None:
            p0 = max(0.50, min(0.95, float(prow[1])))
            p0_source = f"observed({prow[0]})"
        else:
            p0 = 0.70
            p0_source = "fallback:cold_start"
        p1 = min(0.97, p0 + H1_OFFSET)
        rolling = []  # last-N challenger outcomes for the rolling-10 guard
        log_lr = 0.0
        champ_succ = chal_succ = 0
        decision = "continue"
        n = 0
        ti = 0
        while n < args.max_trials and decision == "continue":
            task = ctasks[ti % len(ctasks)]; ti += 1
            # Run both arms on the same task (CACHE-001: cached_run_trial returns the
            # identical (success, score, latency) tuple shape + a cache_state we tally).
            c_ok, c_score, c_lat, c_cs = cached_run_trial(
                conn, args.champion, task, ttl_days=args.cache_ttl_days, force=args.force_rescore)
            h_ok, h_score, h_lat, h_cs = cached_run_trial(
                conn, args.challenger, task, ttl_days=args.cache_ttl_days, force=args.force_rescore)
            cache_stats[c_cs] += 1; cache_stats[h_cs] += 1
            champ_succ += c_ok; chal_succ += h_ok
            # SPRT tracks the CHALLENGER's success stream vs p0/p1
            log_lr = sprt_step(log_lr, h_ok, p0, p1)
            rolling.append(h_ok)
            n += 1
            for model, ok, sc, lat in [(args.champion, c_ok, c_score, c_lat),
                                       (args.challenger, h_ok, h_score, h_lat)]:
                conn.execute(
                    "INSERT INTO bake_off_decisions (round_id, task_class, model_id, rationale, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (round_id, cls, model,
                     json.dumps({"trial_no": n, "score": sc, "success": ok, "latency_ms": lat, "task_id": task["id"]}),
                     now_iso()))
            decision = sprt_decision(log_lr)
            conn.commit()
            print(f"  [{cls}] trial {n}: champ={c_ok}({c_score:.2f}) chal={h_ok}({h_score:.2f}) "
                  f"log_lr={log_lr:.3f} -> {decision}")

        # Research guards (computed BEFORE logging so the verdict can record them):
        # don't promote on a short or noisy run.
        promote_blocked_reason = None
        MIN_PAIRED = 30
        roll10 = rolling[-10:]
        roll_ok = (len(roll10) >= 10 and (sum(roll10) / len(roll10)) > p0)
        guards_pass = (decision == "promote" and n >= MIN_PAIRED and roll_ok)
        if decision == "promote" and not guards_pass:
            promote_blocked_reason = (f"n<{MIN_PAIRED} (got {n})" if n < MIN_PAIRED
                                      else "rolling-10 no longer favours challenger")

        boundary = "promote" if decision=="promote" else ("reject" if decision=="reject" else "cap")
        conn.execute(
            "INSERT INTO sprt_decisions (round_id, task_class, champion_model, challenger_model, "
            "llr, boundary, verdict, concluded_at) VALUES (?,?,?,?,?,?,?,?)",
            (round_id, cls, args.champion, args.challenger, log_lr, boundary,
             json.dumps({"decision": decision, "n_trials": n, "p0": p0, "p0_source": p0_source,
                         "h1": p1, "champ_succ": champ_succ, "chal_succ": chal_succ,
                         "guards_pass": bool(guards_pass) if decision=="promote" else None,
                         "promote_blocked": promote_blocked_reason}), now_iso()))
        if guards_pass and not args.dry_run:
            # dethrone any current champion(s) for this class, then promote
            conn.execute(
                "UPDATE champions SET dethroned_at=? WHERE task_class=? AND dethroned_at IS NULL",
                (now_iso(), cls))
            conn.execute(
                "INSERT INTO champions (task_class, model_id, since_round_id, promoted_at) "
                "VALUES (?,?,?,?)",
                (cls, args.challenger, round_id, now_iso()))
            print(f"  PROMOTED {args.challenger} for {cls} (n={n}, roll10={sum(roll10)}/{len(roll10)})")
        elif promote_blocked_reason:
            print(f"  promote BLOCKED for {cls}: {promote_blocked_reason}")
        conn.commit()
        summary.append((cls, n, champ_succ, chal_succ, decision))
        print(f"  => {cls}: {decision} after {n} trials (champ {champ_succ}/{n}, chal {chal_succ}/{n})")

    conn.execute("UPDATE bake_off_rounds SET status=?, completed_at=? WHERE round_id=?",
                 ("complete" + ("-dryrun" if args.dry_run else ""), now_iso(), round_id))
    conn.commit()
    # CACHE-001: per-round cache summary. Each HIT avoids one model call + JUDGE_SAMPLES
    # judge calls (~1 + N OpenRouter calls saved per hit).
    saved = cache_stats["hit"] * (1 + JUDGE_SAMPLES)
    print(f"cache: {cache_stats['hit']} hits, {cache_stats['miss']} miss, "
          f"{cache_stats['stale']} stale - saved ~{saved} judge calls")
    print(f"\nround {round_id} complete. summary:")
    for cls, n, cs, hs, dec in summary:
        print(f"  {cls}: {dec} (champ {cs}/{n}, chal {hs}/{n})")

if __name__ == "__main__":
    main()
