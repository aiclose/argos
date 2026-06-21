"""
Groomer feedback collector (#709 + #710 + #711).

For each groomer-created sub-task without recent feedback:
- Fetch current Vikunja state
- Compare to initial snapshot (from groomer_subtasks)
- Compute final_state + score
- Insert into groomer_feedback

Run via cron: 0 5 * * * (5am daily).
"""
import sqlite3
import urllib.request
import urllib.error
import ssl
import json
import time
import os

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

ARGOS_DB = "/home/andy/argos/argos.db"
LOG = "/home/andy/logs/groomer-feedback.log"
VIK_API = "https://vikunja-api.aclose.uk/api/v1"
VIK_TOK = "tk_1fee2bf0290a6567e4914f082f8fd285f2b98948"

# How long to observe before final scoring (days)
OBSERVATION_DAYS = 3
# Re-score interval (days) - re-evaluate items still in 'still_open' state
RESCORE_INTERVAL_DAYS = 1


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def vik_get_task(task_id):
    """Return task dict or None if 404."""
    try:
        req = urllib.request.Request(
            f"{VIK_API}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {VIK_TOK}", "User-Agent": "curl/8.0"}
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log(f"  vik HTTP {e.code} for {task_id}: {e}")
        return None
    except Exception as e:
        log(f"  vik error for {task_id}: {e}")
        return None


def char_distance(a, b):
    """Simple absolute char-length difference (edit-distance approximation)."""
    if a is None or b is None:
        return abs(len(a or "") - len(b or ""))
    return abs(len(a) - len(b))


def compute_score(subtask, current):
    """Score from -1.0 (deleted, groomer wrong) to 1.0 (untouched + completed, groomer correct)."""
    if current is None:
        return -1.0, "deleted", {}
    
    init_title = subtask["initial_title"] or ""
    init_desc = subtask["initial_description"] or ""
    init_prio = subtask["initial_priority"]
    
    cur_title = current.get("title") or ""
    cur_desc = current.get("description") or ""
    cur_prio = current.get("priority")
    cur_done = current.get("done", False)
    
    title_changed = init_title.strip() != cur_title.strip()
    desc_changed = init_desc.strip() != cur_desc.strip()
    prio_changed = init_prio != cur_prio
    
    cd_title = char_distance(init_title, cur_title)
    cd_desc = char_distance(init_desc, cur_desc)
    
    changed = title_changed or desc_changed or prio_changed
    
    delta = {
        "title_changed": title_changed,
        "description_changed": desc_changed,
        "priority_changed": prio_changed,
        "char_distance_title": cd_title,
        "char_distance_description": cd_desc,
    }
    
    if cur_done and not changed:
        return 1.0, "completed_unchanged", delta
    if cur_done and changed:
        # Modified but completed - groomer was close
        return 0.7, "completed_modified", delta
    if changed:
        # Open + modified - groomer needed adjustment
        return 0.4, "modified_open", delta
    return 0.0, "still_open_unchanged", delta


def main():
    log("=== groomer feedback collector start ===")
    
    if not os.path.exists(ARGOS_DB):
        log("FATAL: argos.db missing")
        return
    
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.row_factory = sqlite3.Row
    
    # Pull subtasks that need scoring:
    # 1. No feedback row at all and >= OBSERVATION_DAYS old
    # 2. OR feedback exists but final_state is 'still_open_*' and observed >= RESCORE_INTERVAL_DAYS ago
    cutoff_obs = f"datetime('now', '-{OBSERVATION_DAYS} days')"
    cutoff_rescore = f"datetime('now', '-{RESCORE_INTERVAL_DAYS} days')"
    
    rows = list(db.execute(f"""
        SELECT s.subtask_id, s.run_id, s.vikunja_task_id, s.initial_title,
               s.initial_description, s.initial_priority, s.created_at,
               f.feedback_id, f.final_state, f.observed_at
        FROM groomer_subtasks s
        LEFT JOIN groomer_feedback f ON s.subtask_id = f.subtask_id
        WHERE 
            (f.feedback_id IS NULL AND s.created_at <= {cutoff_obs})
            OR
            (f.feedback_id IS NOT NULL 
             AND f.final_state LIKE 'still_open%'
             AND f.observed_at <= {cutoff_rescore})
    """))
    
    log(f"subtasks to score: {len(rows)}")
    
    if not rows:
        log("nothing to score")
        log("=== done ===")
        return
    
    scored = 0
    state_counts = {}
    for r in rows:
        current = vik_get_task(r["vikunja_task_id"])
        score, final_state, delta = compute_score(r, current)
        state_counts[final_state] = state_counts.get(final_state, 0) + 1
        
        if r["feedback_id"]:
            # Update existing
            db.execute("""
                UPDATE groomer_feedback
                SET observed_at = CURRENT_TIMESTAMP, final_state = ?,
                    title_changed = ?, description_changed = ?, priority_changed = ?,
                    char_distance_title = ?, char_distance_description = ?,
                    score = ?
                WHERE feedback_id = ?
            """, (final_state, delta.get("title_changed", False),
                  delta.get("description_changed", False),
                  delta.get("priority_changed", False),
                  delta.get("char_distance_title", 0),
                  delta.get("char_distance_description", 0),
                  score, r["feedback_id"]))
        else:
            # Insert new
            db.execute("""
                INSERT INTO groomer_feedback
                (subtask_id, final_state, title_changed, description_changed, priority_changed,
                 char_distance_title, char_distance_description, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (r["subtask_id"], final_state,
                  delta.get("title_changed", False),
                  delta.get("description_changed", False),
                  delta.get("priority_changed", False),
                  delta.get("char_distance_title", 0),
                  delta.get("char_distance_description", 0),
                  score))
        scored += 1
    
    db.commit()
    log(f"scored {scored} subtasks")
    log(f"state distribution: {state_counts}")
    
    # Compute aggregate metrics for the parent task quality
    avg = db.execute("""
        SELECT AVG(score) avg_score, COUNT(*) n
        FROM groomer_feedback
        WHERE observed_at > datetime('now', '-30 days')
    """).fetchone()
    if avg["n"]:
        log(f"30d aggregate: avg_score={avg['avg_score']:.3f} (n={avg['n']})")
    
    db.close()
    log("=== done ===")


if __name__ == "__main__":
    main()
