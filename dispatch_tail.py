"""Argos dispatch tail - runs every 10 min on garage.

For each NEW cost_log entry since last run:
  1. Classify via Haiku (LiteLLM) into 24 task class taxonomy
  2. Insert into argos.db.dispatches with task_class
  3. Call argos /route to get shadow recommendation
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
ARGOS_URL = "http://127.0.0.1:3020/route"
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
    rows = list(db.execute("""
        SELECT id, ts, tag, model, cost_usd, status, provider_mode, notes
        FROM cost_log
        WHERE id > ?
        ORDER BY id
    """, (last_id,)))
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

            # Insert into dispatches (idempotent)
            try:
                db.execute("""
                    INSERT INTO dispatches
                    (dispatch_id, ts, source, provider_mode, model_used, task_class,
                     actual_cost_usd, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dispatch_id, item['ts'], 'cost_log_tail', item.get('provider_mode'),
                    item.get('model'), klass, item.get('cost_usd'), item.get('status')
                ))
                inserted_dispatches += 1
            except sqlite3.IntegrityError:
                # Already exists - update task_class if missing
                db.execute("UPDATE dispatches SET task_class = ? WHERE dispatch_id = ? AND task_class IS NULL", (klass, dispatch_id))
            db.commit()  # release lock before /route call

            # Call argos /route to get shadow recommendation
            decision = call_argos_route(item['tag'], klass)
            if decision:
                # Compute shadow vs actual cost delta
                predicted = decision.get('predicted_cost_usd', 0)
                actual = item.get('cost_usd', 0) or 0
                delta = (actual - predicted) if predicted else 0
                inserted_predictions += 1
                log(f"  #{item['id']} tag={item['tag'][:30]:30} class={klass:20} predicted=${predicted:.5f} actual=${actual:.5f} delta=${delta:+.5f}")
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
    log(f"  argos /route calls: {inserted_predictions}")
    log(f"  classification failures: {classification_failures}")
    log(f"  last_processed_id: {last_seen_id}")
    log("=== argos dispatch tail done ===")
    db.close()

def _label_new():
    """Derive accepted/quality labels for any newly-inserted dispatches."""
    if outcome_labeler is not None:
        try:
            outcome_labeler.backfill()
        except Exception as e:
            log(f"outcome labeling failed: {e}")

if __name__ == "__main__":
    main()
    _label_new()