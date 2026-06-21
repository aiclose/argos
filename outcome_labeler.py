"""Argos outcome labeler.

Derives the `accepted` label and a `quality_score` from the signals dispatches
already capture, so the quality predictor has real data instead of 2 hand-labelled
rows. This is the honest first step: use the success signal that already exists
before building elaborate new capture.

------------------------------------------------------------------------------
U3-001 rationale (why a GRADUATED heuristic, not an ML multi-factor model)
------------------------------------------------------------------------------
The previous version mapped success->0.8, failure->0.2 - a deliberately crude
2-spike crash proxy. We replace it with a graduated, multi-factor heuristic that
is grounded ONLY in signals that actually exist in the db. Feature availability
(measured across 220 dispatches) is THIN, and that thinness dictates the design:

  status              218/220 present  -> THE MAIN SIGNAL. It has more granularity
                                          than the binary proxy used (10 distinct
                                          values), so we tier it instead of binarising.
  rework_cycles       220/220 present  -> but EVERY value is 0 (zero variance).
                                          Useless today; read defensively so it
                                          penalises IF it ever becomes non-zero,
                                          but it contributes nothing now.
  latency_ms           18/220 present  -> SPARSE. Use only as a mild outlier
                                          adjustment vs a per-class/per-model median,
                                          no-op when absent or median uncomputable.
  actual_output_tokens 22/220 present  -> SPARSE. An output_tokens==0 on a supposedly
                                          successful run is a useful NEGATIVE signal;
                                          no-op when tokens absent.
  error_sensitivity     0/220 present  -> COMPLETELY EMPTY. Not used.

A full ML model trained on these columns would fit noise. The honest, available
improvement is (a) GRADUATED status tiers instead of binary, plus (b) folding in
the sparse signals as SMALL, BOUNDED adjustments ONLY when present, defaulting
gracefully to a no-op otherwise. We do not invent precision the data cannot support.

Status -> (accepted, base quality) tiers:
  completed, ok                       -> (1, 0.85)  clean success
  completed_no_checkpoint             -> (1, 0.70)  finished, no verifiable artifact
  escalated_verify                    -> (0, 0.55)  soft/again: produced work but did
                                                     not cleanly pass (conservative: 0)
  failed_error, errored, failed,
  failed_cost_cap, failed* (catch-all)-> (0, 0.20)  hard failure
  rejected_zdr                        -> (None, None) POLICY-BLOCKED, not a quality
                                                     outcome: blocked before producing
                                                     work, so it is NOT scored at all.
  NULL / unknown                      -> (None, None) no signal

Usable as: backfill (label all existing) and functions dispatch_tail can call.
"""
import sqlite3, datetime, statistics

DB = "/home/andy/argos/argos.db"

# Kept for backward-compat with any importer; status_to_label is the source of truth.
SUCCESS_STATUSES = {"completed", "completed_no_checkpoint", "ok"}
FAILURE_STATUSES = {"errored", "failed_error", "failed_cost_cap"}

# Graduated tier map: status -> (accepted, base_quality). The base_quality is the
# starting point that multi_factor_score() then nudges with sparse adjustments.
# rejected_zdr is intentionally (None, None) - policy-blocked, never quality-judged.
STATUS_TIERS = {
    "completed":               (1, 0.85),   # clean success
    "ok":                      (1, 0.85),   # clean success
    "completed_no_checkpoint": (1, 0.70),   # success-with-caveat (no verifiable artifact)
    "escalated_verify":        (0, 0.55),   # soft/again - did not cleanly pass
    "failed_error":            (0, 0.20),   # hard failure
    "errored":                 (0, 0.20),   # hard failure
    "failed":                  (0, 0.20),   # hard failure
    "failed_cost_cap":         (0, 0.20),   # hard failure
    "rejected_zdr":            (None, None),  # policy-blocked - do NOT score
}

# Bounded adjustment magnitudes (small by design - the signals are sparse/noisy).
EMPTY_OUTPUT_PENALTY = 0.15   # a "successful" run that produced 0 output tokens
LATENCY_OUTLIER_PENALTY = 0.05  # latency > 3x the per-class/per-model median
LATENCY_OUTLIER_FACTOR = 3.0
REWORK_PENALTY_PER_CYCLE = 0.10  # no-op today (all rework_cycles == 0)
REWORK_PENALTY_CAP = 0.30

def status_to_label(status):
    """Return (accepted, base_quality) for a status, or (None, None) if no signal.

    Signature preserved for existing callers. The second element is now a graduated
    base quality (the tier), not the old flat 0.8/0.2 proxy.
    """
    if status in STATUS_TIERS:
        return STATUS_TIERS[status]
    # failed* catch-all (e.g. failed_timeout) -> hard failure
    if status and status.startswith("failed"):
        return 0, 0.20
    return None, None

def multi_factor_score(row, medians=None):
    """Graduated quality score for one dispatch row.

    Starts from the status base tier and applies SMALL, BOUNDED penalties ONLY when
    the relevant signal is present and meaningful. Every adjustment is a strict no-op
    when its signal is absent, so sparse columns never fabricate a score.

    `row` is a mapping with keys: status, actual_output_tokens, latency_ms,
    rework_cycles, task_class, model_used (missing keys treated as absent).
    `medians` is an optional dict from latency_medians(); when None/empty the latency
    adjustment is skipped. Returns a float in [0.0, 1.0], or None for statuses that
    are not quality-judged (rejected_zdr / unknown / NULL).
    """
    accepted, base = status_to_label(row.get("status"))
    if base is None:
        return None  # rejected_zdr / unknown -> not scored
    score = base

    # (a) empty-output: a success that produced nothing is low quality. Only fires
    #     when tokens are actually present (==0), and only on success statuses.
    out = row.get("actual_output_tokens")
    if accepted == 1 and out is not None and out == 0:
        score -= EMPTY_OUTPUT_PENALTY

    # (b) latency outlier: only when latency present AND a per-(task_class or
    #     model_used) median is computable AND this row is a strong outlier.
    lat = row.get("latency_ms")
    if lat is not None and medians:
        med = medians.get(("class", row.get("task_class")))
        if med is None:
            med = medians.get(("model", row.get("model_used")))
        if med and med > 0 and lat > LATENCY_OUTLIER_FACTOR * med:
            score -= LATENCY_OUTLIER_PENALTY

    # (c) rework: no-op today (all values 0). Penalises only if it ever goes >0.
    rw = row.get("rework_cycles")
    if rw is not None and rw > 0:
        score -= min(REWORK_PENALTY_PER_CYCLE * rw, REWORK_PENALTY_CAP)

    return max(0.0, min(1.0, score))

def latency_medians(con):
    """Per-(task_class) and per-(model_used) latency_ms medians, computed in Python
    (sqlite has no median()). Returns {("class", task_class): median, ("model", m): median}.
    Sparse latency means most groups will be absent - that is fine, multi_factor_score
    treats a missing median as "skip the latency adjustment"."""
    medians = {}
    for keytype, col in (("class", "task_class"), ("model", "model_used")):
        groups = {}
        for k, v in con.execute(
                f"SELECT {col}, latency_ms FROM dispatches "
                f"WHERE latency_ms IS NOT NULL AND {col} IS NOT NULL"):
            groups.setdefault(k, []).append(v)
        for k, vals in groups.items():
            if vals:
                medians[(keytype, k)] = statistics.median(vals)
    return medians

def backfill(dry_run=False):
    con = sqlite3.connect(DB)
    medians = latency_medians(con)
    rows = con.execute(
        "SELECT dispatch_id, status, accepted, quality_score, actual_output_tokens, "
        "latency_ms, rework_cycles, task_class, model_used FROM dispatches").fetchall()
    acc_to_label = 0   # rows that gained an accepted label
    q_to_label = 0     # rows that gained a quality_score
    for (did, status, accepted, qscore, out, lat, rw, tc, mu) in rows:
        acc, base = status_to_label(status)
        if base is None:
            continue  # rejected_zdr / unknown -> no quality signal, leave NULL
        need_acc = accepted is None
        need_q = qscore is None
        if not (need_acc or need_q):
            continue  # already fully labelled - don't touch (idempotent)
        q = multi_factor_score(
            {"status": status, "actual_output_tokens": out, "latency_ms": lat,
             "rework_cycles": rw, "task_class": tc, "model_used": mu}, medians)
        if not dry_run:
            # COALESCE: never clobber a real/judged accepted or quality_score.
            con.execute(
                "UPDATE dispatches SET accepted=COALESCE(accepted, ?), "
                "quality_score=COALESCE(quality_score, ?) WHERE dispatch_id=?",
                (acc, q, did))
        if need_acc:
            acc_to_label += 1
        if need_q:
            q_to_label += 1
    if not dry_run:
        con.commit()
    # report
    total = con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    labeled = con.execute("SELECT COUNT(*) FROM dispatches WHERE accepted IS NOT NULL").fetchone()[0]
    pfx = "DRY-RUN: would set" if dry_run else "set"
    print(f"{pfx} accepted on {acc_to_label} rows, quality_score on {q_to_label} rows")
    print(f"dispatches now labelled (accepted): {labeled}/{total}")
    print("--- accept rate by task_class (top, now that labels exist) ---")
    for r in con.execute(
        "SELECT task_class, COUNT(*) n, ROUND(AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END),2) rate "
        "FROM dispatches WHERE accepted IS NOT NULL GROUP BY task_class ORDER BY n DESC LIMIT 8"):
        print("  ", r)
    print("--- accept rate by model_used (the fit signal we wanted) ---")
    for r in con.execute(
        "SELECT model_used, COUNT(*) n, ROUND(AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END),2) rate "
        "FROM dispatches WHERE accepted IS NOT NULL GROUP BY model_used ORDER BY n DESC LIMIT 8"):
        print("  ", r)
    print("--- NEW quality_score distribution (graduated - should be >2 spikes) ---")
    for r in con.execute(
        "SELECT ROUND(quality_score,2) q, COUNT(*) n FROM dispatches "
        "WHERE quality_score IS NOT NULL GROUP BY ROUND(quality_score,2) ORDER BY q"):
        print("  ", r)
    con.close()

if __name__ == "__main__":
    import sys
    backfill(dry_run=("--dry" in sys.argv))
