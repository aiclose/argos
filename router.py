"""Argos Phase 1 shadow router.

Receives task descriptions, picks routing per argos-rules.yaml, logs to predictions table.
SHADOW MODE: does NOT actually dispatch in v0.1.

Listens on 0.0.0.0:3020.
"""
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, List
import yaml
import sqlite3
import time
import uuid
import json
import os
import sys

ARGOS_DB = "/home/andy/argos/argos.db"
RULES_PATH = "/home/andy/argos/argos-rules.yaml"
LOG_PATH = "/home/andy/logs/argos-router.log"

# Load rules at startup; reloaded via /reload-rules
def load_rules():
    with open(RULES_PATH) as f:
        return yaml.safe_load(f)

rules = load_rules()

# ==================== Phase 2 quality predictor (#650) ====================
import pickle
QUALITY_MODEL_PATH = "/home/andy/argos/quality_predictor.pkl"

try:
    import route_select as _rsel
except Exception as _e:
    _rsel = None

_QUALITY_MODEL = None

def _load_quality_model():
    global _QUALITY_MODEL
    try:
        with open(QUALITY_MODEL_PATH, 'rb') as f:
            _QUALITY_MODEL = pickle.load(f)
        return True
    except Exception as e:
        log(f"quality model not loaded: {e}")
        _QUALITY_MODEL = None
        return False

def _extract_features_for_predict(tag, task_class, error_sensitivity, est_in, est_out, notes=""):
    """Mirror of quality_trainer.extract_features (kept inline to avoid import)."""
    import re
    TASK_CLASSES = ["code_generation", "code_implementation", "code_boilerplate", "code_algorithmic",
        "debugging", "debugging_simple", "debugging_intermittent",
        "refactoring", "testing", "test_unit", "test_integration",
        "documentation", "docs_api", "docs_explainer",
        "architecture_design", "data_engineering", "devops", "security",
        "analysis", "formatting", "extraction", "creative", "classification", "conversation"]
    ERROR_SENS = ["low", "medium", "high", "critical"]
    TAG_PATTERNS = [
        ("smoke", re.compile(r"^SMOKE-|TEST-|GATE-", re.I)),
        ("audit", re.compile(r"AUDIT-|CHECK", re.I)),
        ("v2",    re.compile(r"V2-|VAULT-", re.I)),
        ("wave",  re.compile(r"WAVE\d-", re.I)),
        ("argos", re.compile(r"ARGOS", re.I)),
        ("backup",re.compile(r"BACKUP-|RESTORE-", re.I)),
        ("emerg", re.compile(r"CRITICAL|EMERGENCY|FIX", re.I)),
    ]
    one_hot = [0.0]*len(TASK_CLASSES)
    if task_class in TASK_CLASSES:
        one_hot[TASK_CLASSES.index(task_class)] = 1.0
    feats = list(one_hot)
    feats.append(ERROR_SENS.index(error_sensitivity)/3.0 if error_sensitivity in ERROR_SENS else 0.33)
    feats.append(min(est_in/10000.0, 1.0))
    feats.append(min(est_out/5000.0, 1.0))
    feats.append((est_in+est_out)/15000.0)
    feats.append(min(len(tag)/100.0, 1.0))
    for _, pat in TAG_PATTERNS:
        feats.append(1.0 if pat.search(tag or "") else 0.0)
    notes = notes or ""
    feats.append(min(len(notes)/1000.0, 1.0))
    feats.append(1.0 if "OK" in notes or "completed" in notes.lower() else 0.0)
    feats.append(1.0 if "error" in notes.lower() or "failed" in notes.lower() else 0.0)
    return feats

def predict_quality(tag, task_class, error_sensitivity, est_in, est_out):
    """Returns (predicted_success_prob, model_loaded)."""
    if _QUALITY_MODEL is None:
        return None, False
    try:
        clf = _QUALITY_MODEL["classifier"]
        feats = [_extract_features_for_predict(tag, task_class, error_sensitivity, est_in, est_out)]
        prob = float(clf.predict_proba(feats)[0][1])  # P(success)
        return prob, True
    except Exception as e:
        log(f"predict_quality error: {e}")
        return None, False

# Try loading at startup
_load_quality_model()
# ==================== end Phase 2 quality predictor ====================


app_start_time = time.time()

app = FastAPI(title="Argos Phase 1 Shadow Router", version="0.1.0")

# ---- Request / Response models ----

class TaskRequest(BaseModel):
    tag: str = Field(..., description="Unique tag for this task")
    task_class: str = Field(..., description="One of the 24 class_ids in task_classes table")
    error_sensitivity: Optional[str] = Field("medium", description="low/medium/high/critical")
    estimated_input_tokens: int = 1000
    estimated_output_tokens: int = 500
    domain: Optional[str] = None
    task_type: Optional[str] = None
    notes: Optional[str] = None

class RoutingDecision(BaseModel):
    decision_id: str
    tag: str
    shadow_mode: bool
    selected_model: str
    selected_litellm_alias: Optional[str]
    selected_tier: str
    predicted_cost_usd: float
    fallback_chain: List[str]
    decision_rationale: str
    quality_floor_applied: float
    candidate_count: int

# ---- Helpers ----

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def determine_tier(t: TaskRequest):
    routing = rules.get("task_class_routing", {}).get(t.task_class, {})
    tier = routing.get("default_tier", "tier_2")
    # Apply constraints (last-match-wins)
    for constraint in rules.get("constraints", []):
        when = constraint.get("when", {})
        match = True
        for key, expected in when.items():
            if key == "error_sensitivity_above":
                # ordering check
                order = ["low", "medium", "high", "critical"]
                actual = t.error_sensitivity or "medium"
                if order.index(actual) <= order.index(expected):
                    match = False
                    break
            else:
                actual_val = getattr(t, key, None)
                if actual_val != expected:
                    match = False
                    break
        if match and "require_tier" in constraint:
            tier = constraint["require_tier"]
    return tier

def quality_floor(t: TaskRequest) -> float:
    es = t.error_sensitivity or "medium"
    return rules.get("quality_floors", {}).get(es, 0.70)

def get_candidates(tier: str, only_litellm: bool):
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.row_factory = sqlite3.Row
    sql = """
        SELECT model_id, litellm_alias, output_per_1m_usd, input_per_1m_usd, tier
        FROM model_prices
        WHERE tier = ?
          AND deprecated = 0
    """
    params = [tier]
    if only_litellm:
        sql += " AND litellm_alias IS NOT NULL"
    sql += " ORDER BY output_per_1m_usd ASC"
    rows = list(db.execute(sql, params))
    db.close()
    return [dict(r) for r in rows]

def predict_cost(model_row, in_tok: int, out_tok: int) -> float:
    in_p = (model_row.get("input_per_1m_usd") or 0)
    out_p = (model_row.get("output_per_1m_usd") or 0)
    return (in_tok / 1e6) * in_p + (out_tok / 1e6) * out_p

def log_prediction(decision_id: str, tag: str, primary_id: str,
                   candidates: List[str], fallbacks: List[str],
                   cost: float, floor: float, rationale: str):
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.execute("""
        INSERT INTO predictions
        (dispatch_id, predictor_version, predicted_cost_p50, predicted_quality, predicted_success_prob,
         candidate_models, selected_model_id, fallback_chain, decision_rationale, was_exploration)
        VALUES (?, 'phase1-rules-v0.1', ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        f"shadow-{decision_id}-{tag}",
        cost, floor, floor,
        json.dumps(candidates),
        primary_id,
        json.dumps(fallbacks),
        rationale,
    ))
    db.commit()
    db.close()

# ---- Endpoints ----

@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "phase": 1,
        "version": "0.1.0",
        "uptime_sec": round(time.time() - app_start_time, 1),
        "shadow_mode": rules.get("routing_strategy", {}).get("shadow_mode", True),
        "rules_path": RULES_PATH,
    }

@app.post("/route", response_model=RoutingDecision)
def route(t: TaskRequest):
    tier = determine_tier(t)
    floor = quality_floor(t)

    only_litellm = rules.get("routing_strategy", {}).get("prefer_litellm_routable", True)
    candidates = get_candidates(tier, only_litellm)

    fallback_used = False
    if not candidates:
        # Fallback: relax tier constraint, try cheapest litellm-routable
        log(f"  WARN: no candidates in tier={tier} (litellm_only={only_litellm}), falling back")
        candidates = get_candidates("tier_2", only_litellm) or get_candidates("tier_3", only_litellm)
        fallback_used = True

    if not candidates:
        raise HTTPException(503, "No routable models available; check argos.db.model_prices")

    primary = candidates[0]
    fallbacks = [c["model_id"] for c in candidates[1:4]]
    cost = predict_cost(primary, t.estimated_input_tokens, t.estimated_output_tokens)

    rationale_parts = [
        f"task_class={t.task_class}",
        f"tier={tier}" + (" [fallback]" if fallback_used else ""),
        f"error_sensitivity={t.error_sensitivity}",
        f"quality_floor={floor}",
        f"strategy=cheapest-in-tier-with-litellm-alias",
        f"candidates={len(candidates)}",
    ]
    rationale = " | ".join(rationale_parts)

    decision_id = str(uuid.uuid4())[:8]
    # Phase 2: use trained quality predictor if available, else fall back to floor
    pq, _model_loaded = predict_quality(t.tag, t.task_class, t.error_sensitivity, t.estimated_input_tokens, t.estimated_output_tokens)
    predicted_quality_score = pq if pq is not None else floor
    log_prediction(decision_id, t.tag, primary["model_id"],
                   [c["model_id"] for c in candidates[:5]],
                   fallbacks, cost, floor, rationale)
    log(f"ROUTE {t.tag} -> {primary['model_id']} (tier={tier}, ${cost:.5f})")

    return RoutingDecision(
        decision_id=decision_id,
        tag=t.tag,
        shadow_mode=rules.get("routing_strategy", {}).get("shadow_mode", True),
        selected_model=primary["model_id"],
        selected_litellm_alias=primary.get("litellm_alias"),
        selected_tier=tier,
        predicted_cost_usd=round(cost, 6),
        fallback_chain=fallbacks,
        decision_rationale=rationale,
        quality_floor_applied=floor,
        candidate_count=len(candidates),
    )

@app.get("/stats")
def stats():
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.row_factory = sqlite3.Row
    n_predictions = db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    by_model = [dict(r) for r in db.execute("""
        SELECT selected_model_id, COUNT(*) n, ROUND(SUM(predicted_cost_p50), 6) total_cost,
               ROUND(AVG(predicted_cost_p50), 6) mean_cost
        FROM predictions
        GROUP BY selected_model_id
        ORDER BY n DESC
    """)]
    n_classes = db.execute("SELECT COUNT(*) FROM task_classes").fetchone()[0]
    n_models = db.execute("SELECT COUNT(*) FROM model_prices WHERE deprecated = 0").fetchone()[0]
    n_routable = db.execute("SELECT COUNT(*) FROM model_prices WHERE litellm_alias IS NOT NULL AND deprecated = 0").fetchone()[0]
    db.close()
    return {
        "predictions_total": n_predictions,
        "by_model": by_model,
        "task_classes": n_classes,
        "models_active": n_models,
        "models_routable_via_litellm": n_routable,
    }

@app.post("/reload-rules")
def reload_rules():
    global rules
    rules = load_rules()
    return {"ok": True, "rules_loaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}


# ==================== Prometheus /metrics (#683) ====================



@app.post("/predict-quality")
def predict_quality_endpoint(t: TaskRequest):
    """Returns predicted success probability for a given task.
    Phase 2 endpoint - uses trained LogisticRegression on past dispatches.
    """
    pq, model_loaded = predict_quality(t.tag, t.task_class, t.error_sensitivity,
                                        t.estimated_input_tokens, t.estimated_output_tokens)
    if not model_loaded or pq is None:
        return {
            "model_loaded": False,
            "predicted_success_prob": None,
            "fallback_floor": quality_floor(t),
            "note": "model not loaded or prediction failed",
        }
    return {
        "model_loaded": True,
        "predicted_success_prob": round(pq, 4),
        "model_metadata": {
            "trained_at": _QUALITY_MODEL.get("trained_at"),
            "training_size": _QUALITY_MODEL.get("training_size"),
            "training_accuracy": _QUALITY_MODEL.get("training_accuracy"),
        },
    }


@app.post("/reload-quality-model")
def reload_quality_model():
    ok = _load_quality_model()
    return {"ok": ok, "model_loaded": _QUALITY_MODEL is not None}

@app.get("/metrics", response_class=None)
def metrics():
    """Prometheus exposition format. Manual implementation (no prometheus_client dep needed)."""
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.row_factory = sqlite3.Row
    
    n_predictions = db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    n_active = db.execute("SELECT COUNT(*) FROM model_prices WHERE deprecated = 0").fetchone()[0]
    n_routable = db.execute("SELECT COUNT(*) FROM model_prices WHERE litellm_alias IS NOT NULL AND deprecated = 0").fetchone()[0]
    n_dispatches = db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    n_classified = db.execute("SELECT COUNT(*) FROM dispatches WHERE task_class IS NOT NULL").fetchone()[0]
    
    by_tier = {r["tier"]: r["c"] for r in db.execute(
        "SELECT tier, COUNT(*) c FROM model_prices WHERE deprecated = 0 GROUP BY tier"
    )}
    
    by_model_route = list(db.execute("""
        SELECT selected_model_id, COUNT(*) n, SUM(predicted_cost_p50) tc
        FROM predictions
        GROUP BY selected_model_id
    """))
    
    total_predicted_cost = db.execute(
        "SELECT COALESCE(SUM(predicted_cost_p50), 0) FROM predictions"
    ).fetchone()[0]
    
    db.close()
    
    lines = []
    lines.append("# HELP argos_up Argos shadow router up")
    lines.append("# TYPE argos_up gauge")
    lines.append("argos_up 1")
    lines.append("# HELP argos_predictions_total Total routing predictions logged")
    lines.append("# TYPE argos_predictions_total counter")
    lines.append(f"argos_predictions_total {n_predictions}")
    lines.append("# HELP argos_models_active Active models in registry")
    lines.append("# TYPE argos_models_active gauge")
    lines.append(f"argos_models_active {n_active}")
    lines.append("# HELP argos_models_routable_via_litellm Models reachable via LiteLLM")
    lines.append("# TYPE argos_models_routable_via_litellm gauge")
    lines.append(f"argos_models_routable_via_litellm {n_routable}")
    lines.append("# HELP argos_dispatches_total Dispatches recorded")
    lines.append("# TYPE argos_dispatches_total counter")
    lines.append(f"argos_dispatches_total {n_dispatches}")
    lines.append("# HELP argos_dispatches_classified_total Dispatches with task_class")
    lines.append("# TYPE argos_dispatches_classified_total counter")
    lines.append(f"argos_dispatches_classified_total {n_classified}")
    lines.append("# HELP argos_models_by_tier Models grouped by tier")
    lines.append("# TYPE argos_models_by_tier gauge")
    for tier, count in by_tier.items():
        lines.append(f'argos_models_by_tier{{tier="{tier}"}} {count}')
    lines.append("# HELP argos_predicted_cost_usd_total Sum of predicted costs across all predictions")
    lines.append("# TYPE argos_predicted_cost_usd_total counter")
    lines.append(f"argos_predicted_cost_usd_total {total_predicted_cost or 0}")
    lines.append("# HELP argos_route_decisions_total Total route decisions per selected model")
    lines.append("# TYPE argos_route_decisions_total counter")
    for r in by_model_route:
        # sanitize label value
        mid = (r["selected_model_id"] or "unknown").replace('"', '\"')
        lines.append(f'argos_route_decisions_total{{model="{mid}"}} {r["n"]}')
    
    body = "\n".join(lines) + "\n"
    
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4")



# ============ Dispatch record (live write path) ============

_DISPATCHES_COLUMNS = None

def _get_dispatches_columns():
    global _DISPATCHES_COLUMNS
    if _DISPATCHES_COLUMNS is None:
        with sqlite3.connect(ARGOS_DB, timeout=30) as db:
            _DISPATCHES_COLUMNS = [r[1] for r in db.execute("PRAGMA table_info(dispatches)").fetchall()]
    return _DISPATCHES_COLUMNS

@app.post("/dispatch-record")
async def dispatch_record(request: Request):
    from datetime import datetime
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "expected JSON object")
    valid_cols = _get_dispatches_columns()
    filtered = {k: v for k, v in body.items() if k in valid_cols}
    if "dispatch_id" not in filtered or not filtered["dispatch_id"]:
        filtered["dispatch_id"] = str(uuid.uuid4())
    if "ts" not in filtered or not filtered["ts"]:
        filtered["ts"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cols = list(filtered.keys())
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    sql = f"INSERT INTO dispatches ({col_names}) VALUES ({placeholders})"
    try:
        with sqlite3.connect(ARGOS_DB, timeout=30) as db:
            db.execute(sql, [filtered[c] for c in cols])
            db.commit()
    except Exception as e:
        log(f"dispatch-record INSERT error: {e}")
        raise HTTPException(500, str(e))
    return {"ok": True, "dispatch_id": filtered["dispatch_id"]}

# ============ Grading queue (#694) ============
class GradingEnqueueRequest(BaseModel):
    dispatch_id: str
    task_class: str | None = None
    prompt_excerpt: str | None = None
    output_a: str | None = None
    output_b: str | None = None
    model_a: str | None = None
    model_b: str | None = None
    judge_model: str | None = None
    judge_pick: str | None = None
    judge_rationale: str | None = None
    bake_off_round_id: int | None = None


class GradingDecisionRequest(BaseModel):
    queue_id: int
    human_grade: str  # 'A', 'B', 'tie', 'both_bad'
    grader: str | None = "andy"


@app.post("/grading-queue/enqueue")
def grading_enqueue(req: GradingEnqueueRequest):
    """Add an item to the manual grading queue."""
    with sqlite3.connect(ARGOS_DB, timeout=30) as db:
        cur = db.execute("""
            INSERT INTO grading_queue (
                dispatch_id, task_class, prompt_excerpt,
                output_a, output_b, model_a, model_b,
                judge_model, judge_pick, judge_rationale, bake_off_round_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            req.dispatch_id, req.task_class, req.prompt_excerpt,
            req.output_a, req.output_b, req.model_a, req.model_b,
            req.judge_model, req.judge_pick, req.judge_rationale, req.bake_off_round_id
        ))
        db.commit()
        return {"queue_id": cur.lastrowid, "status": "pending"}


@app.get("/grading-queue/next")
def grading_next():
    """Get the next pending grading item."""
    with sqlite3.connect(ARGOS_DB, timeout=30) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""
            SELECT queue_id, dispatch_id, task_class, prompt_excerpt,
                   output_a, output_b, model_a, model_b, judge_pick, judge_rationale, created_at
            FROM grading_queue
            WHERE status = 'pending'
            ORDER BY queue_id ASC
            LIMIT 1
        """).fetchone()
        if not row:
            return {"queue_id": None, "message": "no pending items"}
        return dict(row)


@app.post("/grading-queue/grade")
def grading_grade(req: GradingDecisionRequest):
    """Record human grade + mark item complete."""
    if req.human_grade not in ("A", "B", "tie", "both_bad"):
        raise HTTPException(400, "human_grade must be one of: A, B, tie, both_bad")
    with sqlite3.connect(ARGOS_DB, timeout=30) as db:
        cur = db.execute("""
            UPDATE grading_queue
            SET human_grade = ?, human_grader = ?, status = 'graded', graded_at = CURRENT_TIMESTAMP
            WHERE queue_id = ? AND status = 'pending'
        """, (req.human_grade, req.grader or "andy", req.queue_id))
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "queue_id not found or already graded")
        return {"queue_id": req.queue_id, "human_grade": req.human_grade, "status": "graded"}


@app.get("/grading-queue/stats")
def grading_stats():
    """Stats on the grading queue."""
    with sqlite3.connect(ARGOS_DB, timeout=30) as db:
        pending = db.execute("SELECT COUNT(*) FROM grading_queue WHERE status='pending'").fetchone()[0]
        graded = db.execute("SELECT COUNT(*) FROM grading_queue WHERE status='graded'").fetchone()[0]
        # Distribution of grades
        dist = {}
        for g, c in db.execute("SELECT human_grade, COUNT(*) FROM grading_queue WHERE human_grade IS NOT NULL GROUP BY human_grade"):
            dist[g] = c
        return {"pending": pending, "graded": graded, "grade_distribution": dist}


# ==================== /route-v2: route-aware cost-optimised selection ====================
from fastapi import Body

@app.post("/route-v2")
def route_v2(t: TaskRequest):
    """Route-aware shadow selection over the routes table using effective_cost.
    Picks the cheapest ROUTE whose predicted success clears the task-class floor.
    Still shadow: recommends + logs, does not execute."""
    if _rsel is None:
        raise HTTPException(503, "route_select module not available")
    rt = _rsel.RouteTask(
        tag=t.tag, task_class=t.task_class, error_sensitivity=t.error_sensitivity,
        estimated_input_tokens=t.estimated_input_tokens,
        estimated_output_tokens=t.estimated_output_tokens,
    )
    plan = _rsel.select_route(rt)
    # log to predictions with route-aware rationale
    try:
        db = sqlite3.connect(ARGOS_DB, timeout=30)
        db.execute("""
            INSERT INTO predictions
            (dispatch_id, predictor_version, predicted_cost_p50, predicted_quality,
             predicted_success_prob, candidate_models, selected_model_id,
             fallback_chain, decision_rationale, was_exploration)
            VALUES (?, 'route-v2-cost-optimised', ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            f"shadow-v2-{t.tag}", plan.effective_cost, plan.quality_floor,
            plan.predicted_success, json.dumps([plan.selected_route]),
            plan.selected_route or "", json.dumps(plan.fallback_chain),
            plan.rationale,
        ))
        db.commit(); db.close()
    except Exception as e:
        log(f"route-v2 log error: {e}")
    log(f"ROUTE-V2 {t.tag} -> {plan.selected_route} (eff=${plan.effective_cost}, floor={plan.quality_floor})")
    return {
        "tag": t.tag,
        "shadow_mode": True,
        "selected_route": plan.selected_route,
        "selected_model": plan.selected_model,
        "cost_mode": plan.cost_mode,
        "effective_cost_usd": plan.effective_cost,
        "quality_floor": plan.quality_floor,
        "predicted_success": plan.predicted_success,
        "cleared_floor": plan.cleared_floor,
        "fallback_chain": plan.fallback_chain,
        "candidate_count": plan.candidate_count,
        "rationale": plan.rationale,
    }
