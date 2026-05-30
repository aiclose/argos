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
from __future__ import annotations
import argparse, json, math, os, sqlite3, time, uuid, urllib.request
from datetime import datetime, timezone

OR_KEY = os.getenv("OPENROUTER_API_KEY")  # env-only, no hardcoded secret
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
JUDGE_MODEL = "openai/gpt-oss-120b:free"
PASS_THRESHOLD = 0.7         # judge score >= 0.7 counts as a success (binary outcome for SPRT)

# SPRT params
ALPHA, BETA = 0.05, 0.10
H1_OFFSET = 0.08
LOG_A = math.log((1 - BETA) / ALPHA)   # ~2.77 (promote boundary)
LOG_B = math.log(BETA / (1 - ALPHA))   # ~-1.56 (reject boundary)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def or_chat(model, prompt, max_tokens=600, timeout=90):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.2}).encode()
    req = urllib.request.Request(OR_URL, data=body,
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
        m = d["choices"][0]["message"]
        usage = d.get("usage", {})
        return (m.get("content") or m.get("reasoning") or ""), usage

def judge(task, output, timeout=90):
    """LLM-judge score 0.0-1.0 against the task rubric."""
    prompt = f"""Score this answer 0.0 to 1.0 against the rubric. Reply ONLY a number.

TASK: {task['prompt'][:1500]}
RUBRIC: {task.get('rubric','correctness')}
EXPECTED: {str(task.get('expected',''))[:500]}

ANSWER:
{output[:2500]}

Score (0.0-1.0):"""
    txt, _ = or_chat(JUDGE_MODEL, prompt, max_tokens=20, timeout=timeout)
    import re
    m = re.search(r"(\d*\.?\d+)", txt)
    if not m:
        return 0.0
    return max(0.0, min(1.0, float(m.group(1))))

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
    args = ap.parse_args()

    tasks = json.load(open(args.tasks))
    by_class = {}
    for t in tasks:
        by_class.setdefault(t["category"], []).append(t)

    classes = args.classes.split(",") if args.classes else list(by_class.keys())
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    round_id = conn.execute(
        "INSERT INTO bake_off_rounds (task_class, sample_size, status, started_at, created_at) "
        "VALUES (?,?,?,?,?)",
        ("|".join(classes)[:60], args.max_trials, "running", now_iso(), now_iso())
    ).lastrowid
    conn.commit()
    print(f"round {round_id}: champion={args.champion} challenger={args.challenger}")
    print(f"SPRT boundaries: promote>={LOG_A:.3f} reject<={LOG_B:.3f}")

    summary = []
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
            # Run both arms on the same task
            c_ok, c_score, c_lat = run_trial(args.champion, task)
            h_ok, h_score, h_lat = run_trial(args.challenger, task)
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
    print(f"\nround {round_id} complete. summary:")
    for cls, n, cs, hs, dec in summary:
        print(f"  {cls}: {dec} (champ {cs}/{n}, chal {hs}/{n})")

if __name__ == "__main__":
    main()
