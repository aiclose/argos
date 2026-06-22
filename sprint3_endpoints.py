"""Argos Sprint 3 (CHG-P9-050) endpoint core logic.

Two seams the langgraph-spine needs, kept fastapi-free so they are unit-testable
against a temp DB (router.py wraps them in thin FastAPI handlers):

  * classify_and_route(): the spine has NO task_class at its injection point, so it
    hands Argos raw task_text. We Haiku-classify it (dispatch_tail.classify_one),
    build a RouteTask, call route_select.select_route, and return the RoutePlan plus
    the derived task_class.

  * record_outcome(): the spine feeds realised outcomes back so the learning loop
    closes. We UPSERT into dispatches (idempotent), deriving `accepted` from `status`
    via outcome_labeler.status_to_label (the source of truth), so that
    route_select._predicted_success can promote a route+class to Tier-1 once it has
    >= MIN_OBS observed labels.

SCHEMA NOTE (CHG-P9-050): the dispatches table has NO `tag` column; its PK is
`dispatch_id`. The brief asks for an UPSERT "keyed on tag". We therefore use the
caller's tag verbatim AS the dispatch_id (the same pattern dispatch_tail already
uses, where dispatch_id = f"costlog-{id}"). That is the faithful idempotency key on
the real schema; there is no separate tag column to add.
"""
import sqlite3
from datetime import datetime

import dispatch_tail
import route_select
import outcome_labeler

DB_PATH = "/home/andy/argos/argos.db"
LITELLM_KEY_PATH = "/home/andy/argos/.litellm-key"

# Last-resort task_class when the model returns something outside the taxonomy.
FALLBACK_TASK_CLASS = "conversation"


def _utcnow():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def read_litellm_key(path=LITELLM_KEY_PATH):
    """Best-effort read of the LiteLLM master key; None if absent (caller handles)."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def load_taxonomy(db_path=DB_PATH):
    """Return (classes_str, valid_class_ids) from the task_classes taxonomy table.

    classes_str mirrors the dispatch_tail prompt format ("- <id>: <description>")."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        rows = list(con.execute(
            "SELECT class_id, COALESCE(description, '') AS description "
            "FROM task_classes ORDER BY class_id"))
    finally:
        con.close()
    valid = {r["class_id"] for r in rows}
    classes_str = "\n".join(f"- {r['class_id']}: {r['description']}" for r in rows)
    return classes_str, valid


def _plan_to_dict(plan):
    """RoutePlan -> JSON-safe dict. Keys follow the brief (route_id/model_id/...) and
    stay close to /route-v2's shape so the spine sees a familiar payload."""
    return {
        "route_id": plan.selected_route,
        "model_id": plan.selected_model,
        "cost_mode": plan.cost_mode,
        "effective_cost": plan.effective_cost,
        "quality_floor": plan.quality_floor,
        "predicted_success": plan.predicted_success,
        "cleared_floor": plan.cleared_floor,
        "fallbacks": list(plan.fallback_chain or []),
        "candidate_count": plan.candidate_count,
        "backend": plan.backend,
        "backend_reason": plan.backend_reason,
        "scorer_score": plan.scorer_score,
        "rationale": plan.rationale,
        "error": plan.error,
    }


def classify_and_route(task_text, tag, error_sensitivity=None, predicates=None,
                       db_path=DB_PATH, litellm_key=None, classifier=None):
    """Classify raw task_text -> task_class, then route it. Returns (body, http_code).

    body always carries `task_class` (when classification succeeded) and, on success,
    the flattened RoutePlan. A logical no-route (plan.error set, e.g. quality floor
    not met) is still a 200 with the error in the body, mirroring /route-v2. Only
    infra/classification failures get a non-200 (non-500 where sensible).

    `classifier` is injectable for tests; it defaults to the live Haiku seam
    dispatch_tail.classify_one(task_text, classes_str, litellm_key). Routing always
    reads route_select.DB_PATH; in production that equals db_path.
    """
    if not task_text or not str(task_text).strip():
        return ({"ok": False, "error": {
            "code": "missing_task_text",
            "message": "task_text is required"}}, 400)

    classifier = classifier or dispatch_tail.classify_one

    try:
        classes_str, valid = load_taxonomy(db_path)
    except Exception as e:
        return ({"ok": False, "error": {
            "code": "taxonomy_unavailable",
            "message": f"could not load task taxonomy: {e}"}}, 503)

    if litellm_key is None:
        litellm_key = read_litellm_key()

    try:
        task_class = classifier(task_text, classes_str, litellm_key)
    except Exception as e:
        return ({"ok": False, "error": {
            "code": "classification_failed",
            "message": f"classifier raised: {e}"}}, 502)

    if not task_class:
        return ({"ok": False, "error": {
            "code": "classification_failed",
            "message": "classifier returned no task_class"}}, 502)

    # Keep parity with dispatch_tail: an out-of-taxonomy answer falls back rather
    # than poisoning the route lookup with an unknown class.
    if valid and task_class not in valid:
        task_class = FALLBACK_TASK_CLASS if FALLBACK_TASK_CLASS in valid else task_class

    rt = route_select.RouteTask(
        tag=tag or "",
        task_class=task_class,
        error_sensitivity=error_sensitivity or "medium",
        predicates=predicates,
        prompt=str(task_text),
    )
    try:
        plan = route_select.select_route(rt)
    except Exception as e:
        return ({"ok": False, "task_class": task_class, "error": {
            "code": "routing_failed",
            "message": f"select_route raised: {e}"}}, 502)

    body = _plan_to_dict(plan)
    body["task_class"] = task_class
    body["tag"] = tag
    body["shadow_mode"] = True
    body["ok"] = plan.error is None
    return (body, 200)


def record_outcome(payload, db_path=DB_PATH):
    """Idempotent UPSERT of a realised outcome into dispatches. Returns (body, code).

    Keyed on `tag` (stored AS dispatch_id - see SCHEMA NOTE). `accepted` is derived
    from `status` via outcome_labeler.status_to_label (the source of truth). Updates
    are COALESCE-safe: a None in the payload never null-clobbers an already-populated
    column. A row that already carries a judge's quality_score is NEVER overwritten on
    accepted/quality_score - cost/status/latency may still be refreshed.
    """
    tag = (payload.get("tag") or "").strip()
    if not tag:
        return ({"ok": False, "error": {
            "code": "missing_tag",
            "message": "tag is required (idempotency key)"}}, 400)

    status = payload.get("status")
    task_class = payload.get("task_class")
    cost_usd = payload.get("cost_usd")
    latency_ms = payload.get("latency_ms")
    model_id = payload.get("model_id")
    route_id = payload.get("route_id")

    # Source of truth: never reinvent accepted-from-status. (None, _) for
    # rejected_zdr / unknown statuses -> we leave accepted untouched.
    if status is not None:
        accepted, _base = outcome_labeler.status_to_label(status)
    else:
        accepted = None

    try:
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.row_factory = sqlite3.Row

            # Resolve route_id -> model_used (dispatches has no route_id column).
            model_used = model_id
            if not model_used and route_id:
                r = db.execute(
                    "SELECT model_id FROM routes WHERE route_id=? "
                    "ORDER BY enabled DESC LIMIT 1", (route_id,)).fetchone()
                if r:
                    model_used = r["model_id"]

            existing = db.execute(
                "SELECT dispatch_id, quality_score, accepted, status "
                "FROM dispatches WHERE dispatch_id=?", (tag,)).fetchone()

            if existing is None:
                db.execute(
                    "INSERT INTO dispatches "
                    "(dispatch_id, ts, source, model_used, task_class, "
                    " actual_cost_usd, latency_ms, status, accepted) "
                    "VALUES (?, ?, 'spine_outcome', ?, ?, ?, ?, ?, ?)",
                    (tag, _utcnow(), model_used, task_class,
                     cost_usd, latency_ms, status, accepted))
                db.commit()
                return ({"ok": True, "action": "insert", "dispatch_id": tag,
                         "accepted": accepted, "model_used": model_used,
                         "task_class": task_class, "status": status}, 200)

            judged = existing["quality_score"] is not None
            if judged:
                # Preserve the judge's verdict; only refresh observational fields.
                db.execute(
                    "UPDATE dispatches SET "
                    "  actual_cost_usd = COALESCE(?, actual_cost_usd), "
                    "  latency_ms      = COALESCE(?, latency_ms), "
                    "  status          = COALESCE(?, status), "
                    "  model_used      = COALESCE(?, model_used), "
                    "  task_class      = COALESCE(?, task_class) "
                    "WHERE dispatch_id=?",
                    (cost_usd, latency_ms, status, model_used, task_class, tag))
                db.commit()
                return ({"ok": True, "action": "update_preserve_judged",
                         "dispatch_id": tag, "judged": True,
                         "accepted": existing["accepted"]}, 200)

            db.execute(
                "UPDATE dispatches SET "
                "  actual_cost_usd = COALESCE(?, actual_cost_usd), "
                "  latency_ms      = COALESCE(?, latency_ms), "
                "  status          = COALESCE(?, status), "
                "  model_used      = COALESCE(?, model_used), "
                "  task_class      = COALESCE(?, task_class), "
                "  accepted        = COALESCE(?, accepted) "
                "WHERE dispatch_id=?",
                (cost_usd, latency_ms, status, model_used, task_class, accepted, tag))
            db.commit()
            eff_accepted = accepted if accepted is not None else existing["accepted"]
            return ({"ok": True, "action": "update", "dispatch_id": tag,
                     "accepted": eff_accepted, "judged": False}, 200)
    except Exception as e:
        return ({"ok": False, "error": {
            "code": "outcome_write_failed", "message": str(e)}}, 500)
