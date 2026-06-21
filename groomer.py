"""Phase 1 Backlog Groomer (#649).

Polls Vikunja project 24 (Active) for tasks containing 'AUTO-GROOM' marker in
description. For each:
  1. Send title + description to Haiku via LiteLLM
  2. Parse decomposition (3-5 sub-tasks JSON array)
  3. Create sub-tasks in Vikunja with parent_task_id link
  4. Update parent description: replace AUTO-GROOM with GROOMED+timestamp
  5. Add comment to parent listing the new sub-task IDs

Runs every 30 min via cron on garage.
"""
import urllib.request
import urllib.error
import json
import ssl
import time
import os
import sqlite3
import sys
import re

VIKUNJA_API = "https://vikunja-api.aclose.uk/api/v1"
VIKUNJA_TOKEN = "tk_1fee2bf0290a6567e4914f082f8fd285f2b98948"
LITELLM_URL = "http://192.168.4.10:4000/v1/chat/completions"
LITELLM_KEY_FILE = "/home/andy/argos/.litellm-key"
ACTIVE_PROJECT_ID = 24
LOG_PATH = "/home/andy/logs/argos-groomer.log"

GROOM_MARKER = "AUTO-GROOM"  # description line starting with this triggers grooming
GROOMED_MARKER_PREFIX = "GROOMED-"  # replaces AUTO-GROOM after grooming

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def vik(path, method="GET", body=None):
    h = {"Authorization": f"Bearer {VIKUNJA_TOKEN}", "Content-Type": "application/json", "User-Agent": "curl/8.0"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{VIKUNJA_API}{path}", data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.loads(r.read() or b"{}")

def find_groomable_tasks():
    """Returns list of tasks where description contains AUTO-GROOM marker AND not yet groomed."""
    tasks = []
    page = 1
    while True:
        batch = vik(f"/projects/{ACTIVE_PROJECT_ID}/tasks?per_page=50&page={page}")
        if not batch:
            break
        for t in batch:
            desc = t.get("description") or ""
            # Strip HTML tags Vikunja sometimes inserts
            desc_plain = re.sub(r'<[^>]+>', '', desc)
            if GROOM_MARKER in desc_plain and GROOMED_MARKER_PREFIX not in desc_plain:
                tasks.append(t)
        if len(batch) < 50:
            break
        page += 1
    return tasks

def haiku_decompose(title, description, key):
    """Ask Haiku to decompose a task into 3-5 actionable sub-tasks."""
    prompt = f"""You are a homelab project planner. Given a parent task, decompose it into 3-5 actionable sub-tasks.

Each sub-task must be:
- Concrete (specific deliverable, not vague)
- Independently completable in <2 hours
- Worded as a TODO ("Add X...", "Configure Y...", "Verify Z...")

Parent task title: {title}

Parent task description:
{description}

Respond with ONLY a JSON array of sub-task objects, each with:
  - title: short imperative title (max 80 chars)
  - description: 1-3 sentences of context/scope
  - estimate_minutes: rough time estimate (15-120)
  - priority: 1-4 (1=lowest, 4=highest, default 3)

Example response (no prose, no markdown fences):
[{{"title":"Add /metrics endpoint to argos","description":"Expose Prometheus format metrics on port 3020. Use prometheus_client.","estimate_minutes":45,"priority":3}}]
"""
    body = json.dumps({
        "model": "claude-haiku",
        "messages": [
            {"role": "system", "content": "You are a task decomposition assistant. Respond ONLY with a JSON array."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 2000,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(LITELLM_URL, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "curl/8.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            resp = json.load(r)
        content = resp["choices"][0]["message"]["content"].strip()
        # Strip code fences if Haiku added them
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content), resp.get("usage", {}).get("cost", 0)
    except Exception as e:
        log(f"  haiku error: {e}")
        return None, 0

def create_subtask(parent_id, sub):
    """Create a sub-task linked to parent via task_relation."""
    # Step 1: create task in same project
    task_body = {
        "title": sub.get("title", "")[:200],
        "description": sub.get("description", ""),
        "priority": int(sub.get("priority", 3)),
    }
    new = vik(f"/projects/{ACTIVE_PROJECT_ID}/tasks", method="PUT", body=task_body)
    new_id = new.get("id")
    if not new_id:
        return None
    
    # Step 2: link as subtask via Vikunja relation 'parenttask'
    try:
        vik(f"/tasks/{new_id}/relations", method="PUT", body={
            "task_id": new_id,
            "other_task_id": parent_id,
            "relation_kind": "parenttask"
        })
    except Exception as e:
        log(f"  warning: relation failed for new task {new_id} -> parent {parent_id}: {e}")
        # Task still exists, just unlinked
    
    return new_id

def update_parent(parent, sub_ids, cost):
    """Replace AUTO-GROOM marker with GROOMED-<timestamp> + add comment."""
    desc = parent.get("description") or ""
    ts = time.strftime("%Y-%m-%d %H:%M")
    new_desc = desc.replace(GROOM_MARKER, f"{GROOMED_MARKER_PREFIX}{ts}")
    
    try:
        vik(f"/tasks/{parent['id']}", method="POST", body={
            "id": parent['id'],
            "description": new_desc,
        })
    except Exception as e:
        log(f"  warning: parent description update failed: {e}")
    
    # Add comment
    sub_links = "\n".join([f"- #{sid}" for sid in sub_ids if sid])
    comment = f"""🤖 Auto-groomed {ts} via Haiku.

Decomposed into {len(sub_ids)} sub-tasks:
{sub_links}

Cost: ${cost:.5f} (Haiku via LiteLLM)
"""
    try:
        vik(f"/tasks/{parent['id']}/comments", method="PUT", body={"comment": comment})
    except Exception as e:
        log(f"  warning: comment failed: {e}")



# ============ groomer_runs / groomer_subtasks tracking (#708/#710) ============
ARGOS_DB = "/home/andy/argos/argos.db"

def record_run(parent_task, n_subtasks, judge_model, cost, rationale=""):
    """Insert a row into groomer_runs and return run_id."""
    try:
        with sqlite3.connect(ARGOS_DB, timeout=30) as db:
            cur = db.execute(
                """INSERT INTO groomer_runs
                   (parent_task_id, parent_title, parent_description_excerpt,
                    n_subtasks_created, judge_model, cost_usd, rationale)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (parent_task.get("id"),
                 (parent_task.get("title") or "")[:500],
                 (parent_task.get("description") or "")[:2000],
                 n_subtasks, judge_model, cost, rationale[:2000])
            )
            db.commit()
            return cur.lastrowid
    except Exception as e:
        log(f"  WARN: failed to record run: {e}")
        return None


def record_subtask(run_id, vikunja_task_id, sub_spec):
    """Insert a row into groomer_subtasks snapshotting initial state."""
    if not run_id:
        return
    try:
        with sqlite3.connect(ARGOS_DB, timeout=30) as db:
            db.execute(
                """INSERT INTO groomer_subtasks
                   (run_id, vikunja_task_id, initial_title, initial_description, initial_priority)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, vikunja_task_id,
                 (sub_spec.get("title") or "")[:500],
                 (sub_spec.get("description") or "")[:5000],
                 sub_spec.get("priority"))
            )
            db.commit()
    except Exception as e:
        log(f"  WARN: failed to record subtask: {e}")

def main():
    log("=== argos groomer start ===")
    
    # Load LiteLLM key
    if not os.path.exists(LITELLM_KEY_FILE):
        log(f"FATAL: {LITELLM_KEY_FILE} missing")
        sys.exit(1)
    with open(LITELLM_KEY_FILE) as f:
        litellm_key = f.read().strip()
    
    # Find groomable tasks
    tasks = find_groomable_tasks()
    log(f"groomable tasks (containing AUTO-GROOM): {len(tasks)}")
    
    if not tasks:
        log("nothing to do")
        return
    
    total_subs = 0
    total_cost = 0
    
    for t in tasks:
        log(f"grooming #{t['id']} {t['title'][:60]}")
        sub_specs, cost = haiku_decompose(t['title'], t.get('description', ''), litellm_key)
        if not sub_specs:
            log(f"  decompose failed, skipping")
            continue
        
        log(f"  Haiku produced {len(sub_specs)} sub-tasks (cost ${cost:.5f})")
        # Record this run (#708)
        run_id = record_run(t, len(sub_specs), "anthropic/claude-haiku-4.5", cost)
        sub_ids = []
        for sub in sub_specs:
            sid = create_subtask(t['id'], sub)
            if sid:
                sub_ids.append(sid)
                # Snapshot subtask initial state (#708)
                record_subtask(run_id, sid, sub)
                log(f"    +#{sid} {sub.get('title','')[:60]}")
        
        update_parent(t, sub_ids, cost)
        total_subs += len(sub_ids)
        total_cost += cost
    
    log("")
    log(f"=== summary ===")
    log(f"  parents groomed: {len(tasks)}")
    log(f"  sub-tasks created: {total_subs}")
    log(f"  total cost: ${total_cost:.5f}")
    log("=== argos groomer done ===")

if __name__ == "__main__":
    main()
