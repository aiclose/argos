"""Argos dispatch tail - runs every 10 min on garage.

For each NEW cost_log entry since last run:
  1. Classify via Haiku (LiteLLM) into 24 task class taxonomy
  2. Insert into argos.db.dispatches with task_class
  3. Call argos /route-v2 to get the shadow recommendation (route-aware)
  4. Compute shadow_vs_actual cost delta
  5. Log to predictions table (linked to dispatch_id)

This combines:
- #682 DISPATCH-CLASSIFICATION-AUTO
- #679 PHASE-1-V02-DISPATCH-HOOK (shadow compare path)
"""

import sqlite3
import urllib.request
import urllib.error
import json
import ssl
import time
import os
import re
import sys
import shutil
try:
    import outcome_labeler
except Exception:
    outcome_labeler = None

ARGOS_DB = "/home/andy/argos/argos.db"
COST_LOG_REMOTE_USER = "andy@192.168.4.10"
COST_LOG_REMOTE_PATH = "/home/andy/orchestrator/cost-log.db"
COST_LOG_LOCAL_SNAPSHOT = "/tmp/cost_log_snapshot.db"
LITELLM_URL = "http://192.168.4.10:4000/v1/chat/completions"
# ARGOS-S2 FIX 2a: point at the REAL router endpoint. There is NO /route endpoint
# (router.py exposes only /route-v2); the old bare /route was a dead URL, so every
# shadow-recommendation call errored out and NO daily evidence was logged.
ARGOS_URL = "http://127.0.0.1:3020/route-v2"
LOG_PATH = "/home/andy/logs/argos-dispatch-tail.log"
STATE_FILE = "/home/andy/argos/dispatch-tail-state.json"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

_TOKENS_RE = re.compile(r"in\s*=\s*([\d,]+).*?out\s*=\s*([\d,]+)", re.IGNORECASE | re.DOTALL)

def parse_tokens(notes):
    """Extract (input_tokens, output_tokens) from a free-text notes field.

    Looks for the pattern 'tokens in=50 out=20' (case-insensitive, tolerant of
    extra text/whitespace and commas in the numbers). Returns (None, None) if it
    cannot parse.
    """
    if not notes:
        return (None, None)
    m = _TOKENS_RE.search(notes)
    if not m:
        return (None, None)
    try:
        tin = int(m.group(1).replace(",", ""))
        tout = int(m.group(2).replace(",", ""))
        return (tin, tout)
    except (ValueError, AttributeError):
        return (None, None)

def get_state():
    if not os.path.exists(STATE_FILE):
        return {"last_processed_id": 0}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_processed_id": 0}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def fetch_new_cost_log_entries(last_id):
    """Pull cost_log snapshot from UM780 via scp, return entries with id > last_id."""
    rc = os.system(f"scp -q {COST_LOG_REMOTE_USER}:{COST_LOG_REMOTE_PATH} {COST_LOG_LOCAL_SNAPSHOT} 2>/dev/null")
    if rc != 0:
        log(f"FATAL: scp failed (rc={rc})")
        return []
    db = sqlite3.connect(COST_LOG_LOCAL_SNAPSHOT)
    db.row_factory = sqlite3.Row
    # Prefer structured token columns when the snapshot has them (orchestrator >= U1b);
    # fall back to the legacy column set for older snapshots.
    _cl_cols = {r[1] for r in db.execute("PRAGMA table_info(cost_log)")}
    if "prompt_tokens" in _cl_cols and "completion_tokens" in _cl_cols:
        _sel = ("SELECT id, ts, tag, model, cost_usd, status, provider_mode, notes, "
                "prompt_tokens, completion_tokens FROM cost_log WHERE id > ? ORDER BY id")
    else:
        _sel = ("SELECT id, ts, tag, model, cost_usd, status, provider_mode, notes "
                "FROM cost_log WHERE id > ? ORDER BY id")
    rows = list(db.execute(_sel, (last_id,)))
    db.close()
    return [dict(r) for r in rows]

def classify_batch(items, classes_str, litellm_key):
    """Send batch to Haiku, get list of class_ids back."""
    items_text = "\n".join([
        f"{i+1}. [{x['tag']}] {x.get('notes') or '(no notes)'}"
        for i, x in enumerate(items)
    ])
    prompt = f"""Classify each homelab task into one of these classes:

{classes_str}

Tasks to classify:
{items_text}

Respond with ONLY a JSON array of class_ids in order, one per task. No prose.
Example: ["devops", "testing", ...]
"""
    body = json.dumps({
        "model": "claude-haiku",
        "messages": [
            {"role": "system", "content": "You are a task classifier. Respond ONLY with a JSON array."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        LITELLM_URL, data=body,
        headers={"Authorization": f"Bearer {litellm_key}", "Content-Type": "application/json", "User-Agent": "curl/8.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            resp = json.load(r)
        content = resp["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content), resp.get("usage", {})
    except Exception as e:
        log(f"  classify error: {e}")
        return None, None

def call_argos_route(tag, task_class):
    # Payload already matches TaskRequest, which /route-v2 also consumes - keep as-is.
    body = json.dumps({
        "tag": tag,
        "task_class": task_class,
        "error_sensitivity": "medium",
        "estimated_input_tokens": 2000,
        "estimated_output_tokens": 1500,
    }).encode()
    req = urllib.request.Request(
        ARGOS_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "curl/8.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.load(r)
    except Exception as e:
        log(f"  argos route error: {e}")
        return None

def insert_prediction(db, dispatch_id, decision):
    """Map a /route-v2 response into a predictions row linked to the real dispatch_id.

    Honest mapping note (ARGOS-S2 2a): /route-v2 is route_select.select_route's
    route-aware plan, NOT the legacy /route RoutingDecision. The live response
    field names differ, so we map the REAL ones:
        selected_model_id      <- selected_model (fallback: selected_route)
        predicted_cost_p50     <- effective_cost_usd  (p90/p95 left NULL: not provided)
        predicted_success_prob <- predicted_success
        predicted_quality      <- quality_floor
        fallback_chain         <- json(fallback_chain)
        decision_rationale     <- rationale
        predictor_version       = "route-v2-cost-optimised" (+"-noroute" if no route)

    If /route-v2 returned an error or an empty selected_model we still insert a
    row (predictor_version suffixed "-noroute", rationale carrying the reason) so
    the absence of a route is itself recorded evidence. Best-effort: logs and
    returns False on any DB error, never raises into the tail loop.
    """
    err = decision.get("error")
    sel_model = decision.get("selected_model") or decision.get("selected_route")
    cost = decision.get("effective_cost_usd")
    psucc = decision.get("predicted_success")
    quality = decision.get("quality_floor")
    rationale = decision.get("rationale") or ""
    fallbacks = decision.get("fallback_chain") or []
    no_route = bool(err) or not sel_model
    version = "route-v2-cost-optimised" + ("-noroute" if no_route else "")
    if no_route:
        reason = json.dumps(err) if err else "empty selected_model"
        rationale = f"NO ROUTE ({reason})" + (f" | {rationale}" if rationale else "")
        sel_model = None
    try:
        # Same column set the router's own log_prediction uses (proven against the
        # live predictions schema); p90/p95/created_at default at the DB layer.
        db.execute("""
            INSERT INTO predictions
            (dispatch_id, predictor_version, predicted_cost_p50, predicted_quality,
             predicted_success_prob, candidate_models, selected_model_id,
             fallback_chain, decision_rationale, was_exploration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            dispatch_id, version, cost, quality, psucc,
            json.dumps([sel_model] if sel_model else []),
            sel_model, json.dumps(fallbacks), rationale,
        ))
        db.commit()
        return True
    except Exception as e:
        log(f"  prediction insert error for {dispatch_id}: {e}")
        return False

def main():
    log("=== argos dispatch tail start ===")

    # Load LiteLLM key
    litellm_key = None
    try:
        with open("/home/andy/argos/.litellm-key") as f:
            litellm_key = f.read().strip()
    except FileNotFoundError:
        log("FATAL: /home/andy/argos/.litellm-key missing - create with the master key")
        sys.exit(1)

    state = get_state()
    last_id = state.get("last_processed_id", 0)
    log(f"last_processed_id: {last_id}")

    new_entries = fetch_new_cost_log_entries(last_id)
    log(f"new entries since last run: {len(new_entries)}")

    if not new_entries:
        log("nothing to do")
        return

    db = sqlite3.connect(ARGOS_DB, timeout=30); db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row

    # Load taxonomy for the prompt
    classes = list(db.execute("SELECT class_id, description FROM task_classes ORDER BY class_id"))
    valid_class_ids = {r['class_id'] for r in classes}
    classes_str = "\n".join([f"- {r['class_id']}: {r['description']}" for r in classes])

    # Process in batches of 20
    inserted_dispatches = 0
    inserted_predictions = 0
    classification_failures = 0
    BATCH_SIZE = 20
    last_seen_id = last_id

    for batch_start in range(0, len(new_entries), BATCH_SIZE):
        batch = new_entries[batch_start:batch_start + BATCH_SIZE]
        log(f"batch {batch_start//BATCH_SIZE + 1}: items {batch_start+1}-{batch_start+len(batch)}")

        result, usage = classify_batch(batch, classes_str, litellm_key)
        if result is None:
            classification_failures += len(batch)
            log(f"  batch failed, skipping")
            continue

        for item, klass in zip(batch, result):
            if klass not in valid_class_ids:
                klass = "conversation"  # fallback
            
            dispatch_id = f"costlog-{item['id']}"
            # Prefer structured token columns from cost_log; fall back to parsing notes.
            tin = item.get('prompt_tokens')
            tout = item.get('completion_tokens')
            if tin is None and tout is None:
                tin, tout = parse_tokens(item.get('notes'))

            # Insert into dispatches (idempotent)
            try:
                db.execute("""
                    INSERT INTO dispatches
                    (dispatch_id, ts, source, provider_mode, model_used, task_class,
                     actual_cost_usd, status, actual_input_tokens, actual_output_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dispatch_id, item['ts'], 'cost_log_tail', item.get('provider_mode'),
                    item.get('model'), klass, item.get('cost_usd'), item.get('status'),
                    tin, tout
                ))
                inserted_dispatches += 1
            except sqlite3.IntegrityError:
                # Already exists - update task_class if missing
                db.execute("UPDATE dispatches SET task_class = ? WHERE dispatch_id = ? AND task_class IS NULL", (klass, dispatch_id))
                # Also backfill tokens if currently missing (mirror the task_class update)
                if tin is not None or tout is not None:
                    db.execute(
                        "UPDATE dispatches SET actual_input_tokens = ?, actual_output_tokens = ? "
                        "WHERE dispatch_id = ? AND (actual_input_tokens IS NULL OR actual_input_tokens = 0)",
                        (tin, tout, dispatch_id)
                    )
            db.commit()  # release lock before /route call

            # Call argos /route-v2 to get the shadow recommendation, then log the
            # prediction row linked to THIS dispatch_id (the honest daily evidence).
            decision = call_argos_route(item['tag'], klass)
            if decision:
                logged = insert_prediction(db, dispatch_id, decision)
                if logged:
                    inserted_predictions += 1
                sel_model = decision.get('selected_model') or decision.get('selected_route')
                if decision.get('error') or not sel_model:
                    log(f"  #{item['id']} tag={item['tag'][:30]:30} class={klass:20} (route-v2 no route: {decision.get('error')})")
                else:
                    # Compute shadow vs actual cost delta (effective_cost is the route-aware shadow price)
                    predicted = decision.get('effective_cost_usd') or 0
                    actual = item.get('cost_usd', 0) or 0
                    delta = (actual - predicted) if predicted else 0
                    log(f"  #{item['id']} tag={item['tag'][:30]:30} class={klass:20} model={str(sel_model)[:24]:24} predicted=${predicted:.5f} actual=${actual:.5f} delta=${delta:+.5f}")
            else:
                log(f"  #{item['id']} tag={item['tag'][:30]:30} class={klass:20} (route call failed)")

            last_seen_id = max(last_seen_id, item['id'])

        db.commit()

    # Update state
    state["last_processed_id"] = last_seen_id
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

    log("")
    log(f"=== summary ===")
    log(f"  new cost_log entries: {len(new_entries)}")
    log(f"  inserted into dispatches: {inserted_dispatches}")
    log(f"  predictions logged (/route-v2): {inserted_predictions}")
    log(f"  classification failures: {classification_failures}")
    log(f"  last_processed_id: {last_seen_id}")
    log("=== argos dispatch tail done ===")
    db.close()

def backfill_tokens_from_notes():
    """One-time backfill: populate actual_input/output_tokens for existing
    cost_log_tail dispatches by re-reading the cost_log notes field.

    Scans dispatches where tokens are missing (NULL or 0), looks up the matching
    cost_log entry via the dispatch_id pattern 'costlog-<id>', parses notes, and
    updates the row. Safe to run repeatedly (only touches still-missing rows).
    """
    log("=== argos token backfill start ===")

    # Use an existing snapshot if present, else pull a fresh one via scp.
    if os.path.exists(COST_LOG_LOCAL_SNAPSHOT):
        log(f"using existing cost_log snapshot: {COST_LOG_LOCAL_SNAPSHOT}")
    else:
        rc = os.system(f"scp -q {COST_LOG_REMOTE_USER}:{COST_LOG_REMOTE_PATH} {COST_LOG_LOCAL_SNAPSHOT} 2>/dev/null")
        if rc != 0:
            log(f"FATAL: scp failed (rc={rc})")
            return
        log(f"pulled fresh cost_log snapshot: {COST_LOG_LOCAL_SNAPSHOT}")

    cl = sqlite3.connect(COST_LOG_LOCAL_SNAPSHOT)
    cl.row_factory = sqlite3.Row
    notes_by_id = {r['id']: r['notes'] for r in cl.execute("SELECT id, notes FROM cost_log")}
    cl.close()
    log(f"cost_log entries available: {len(notes_by_id)}")

    db = sqlite3.connect(ARGOS_DB, timeout=30); db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    rows = list(db.execute(
        "SELECT dispatch_id FROM dispatches "
        "WHERE actual_input_tokens IS NULL OR actual_input_tokens = 0"
    ))
    log(f"dispatches missing tokens: {len(rows)}")

    updated = 0
    skipped = 0
    for r in rows:
        did = r['dispatch_id']
        if not did or not did.startswith("costlog-"):
            skipped += 1
            continue
        try:
            cid = int(did[len("costlog-"):])
        except ValueError:
            skipped += 1
            continue
        notes = notes_by_id.get(cid)
        tin, tout = parse_tokens(notes)
        if tin is None and tout is None:
            skipped += 1
            continue
        db.execute(
            "UPDATE dispatches SET actual_input_tokens = ?, actual_output_tokens = ? "
            "WHERE dispatch_id = ? AND (actual_input_tokens IS NULL OR actual_input_tokens = 0)",
            (tin, tout, did)
        )
        updated += 1
    db.commit()
    db.close()

    log(f"  tokens backfilled: {updated}")
    log(f"  skipped (no match/unparseable): {skipped}")
    log("=== argos token backfill done ===")

def _label_new():
    """Derive accepted/quality labels for any newly-inserted dispatches."""
    if outcome_labeler is not None:
        try:
            outcome_labeler.backfill()
        except Exception as e:
            log(f"outcome labeling failed: {e}")

if __name__ == "__main__":
    if "--backfill-tokens" in sys.argv:
        backfill_tokens_from_notes()
    else:
        main()
        _label_new()