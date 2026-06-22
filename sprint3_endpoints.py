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
