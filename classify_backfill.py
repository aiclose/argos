"""Argos #677: classify 116 backfilled dispatches via Haiku through LiteLLM."""
import sqlite3
import urllib.request
import urllib.error
import json
import ssl
import time
import sys

ARGOS_DB = "/home/andy/argos/argos.db"
LITELLM_URL = "http://192.168.4.10:4000/v1/chat/completions"
LITELLM_KEY = sys.argv[1]  # passed as arg
BATCH_SIZE = 20

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def get_classes(db):
    return [dict(r) for r in db.execute("SELECT class_id, description FROM task_classes ORDER BY class_id")]

def get_unclassified(db):
    return [dict(r) for r in db.execute("""
        SELECT dispatch_id, ts, source, provider_mode, model_used, status,
               actual_cost_usd, actual_input_tokens, actual_output_tokens
        FROM dispatches
        WHERE task_class IS NULL
        ORDER BY ts
    """)]

def get_dispatch_context(db, dispatch_id):
    """Pull the original tag/notes from cost_log to give the classifier context."""
    # We're on garage, cost_log is on UM780 - use the snapshot we copied during backfill
    snap = "/tmp/cost_log_snapshot.db"
    cdb = sqlite3.connect(snap)
    cdb.row_factory = sqlite3.Row
    # dispatch_id format: costlog-{id}
    if not dispatch_id.startswith("costlog-"):
        cdb.close()
        return None
    cl_id = dispatch_id.split("-", 1)[1]
    row = cdb.execute("SELECT tag, notes FROM cost_log WHERE id = ?", (cl_id,)).fetchone()
    cdb.close()
    return dict(row) if row else None

def classify_batch(items, classes, model="claude-haiku"):
    """Send a batch to LiteLLM/Haiku, get back classifications."""
    class_lines = "\n".join([f"- {c['class_id']}: {c['description']}" for c in classes])
    
    items_text = "\n".join([
        f"{i+1}. [{x['tag']}] {x['notes'] or '(no notes)'}"
        for i, x in enumerate(items)
    ])

    prompt = f"""Classify each homelab task into one of these classes:

{class_lines}

Tasks to classify:
{items_text}

Respond with ONLY a JSON array of class_ids in order, one per task. No prose, no explanation.
Example: ["devops", "testing", "code_implementation", ...]
"""

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a task classifier. Respond ONLY with a JSON array."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        LITELLM_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "argos-classify/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            resp = json.load(r)
        content = resp["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        result = json.loads(content)
        usage = resp.get("usage", {})
        return result, usage
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        log(f"  HTTP {e.code}: {err[:200]}")
        return None, None
    except Exception as e:
        log(f"  Error: {e}")
        return None, None

def main():
    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row

    classes = get_classes(db)
    valid_class_ids = {c['class_id'] for c in classes}
    log(f"Loaded {len(classes)} task classes")

    unclass = get_unclassified(db)
    log(f"Found {len(unclass)} unclassified dispatches")

    if not unclass:
        log("Nothing to do.")
        return

    # Enrich each with tag/notes from cost_log snapshot
    enriched = []
    for d in unclass:
        ctx_data = get_dispatch_context(db, d['dispatch_id'])
        if ctx_data:
            d.update(ctx_data)
        else:
            d['tag'] = '(unknown)'
            d['notes'] = ''
        enriched.append(d)

    # Process in batches
    total_in_tokens = 0
    total_out_tokens = 0
    classified_count = 0
    invalid_count = 0
    class_counts = {}

    for batch_start in range(0, len(enriched), BATCH_SIZE):
        batch = enriched[batch_start:batch_start + BATCH_SIZE]
        log(f"Batch {batch_start//BATCH_SIZE + 1}: items {batch_start+1}-{batch_start+len(batch)}")

        result, usage = classify_batch(batch, classes)
        if result is None:
            log("  batch failed, skipping")
            continue

        if usage:
            total_in_tokens += usage.get('prompt_tokens', 0)
            total_out_tokens += usage.get('completion_tokens', 0)

        if len(result) != len(batch):
            log(f"  WARN: returned {len(result)} classifications for {len(batch)} items")
        
        for item, klass in zip(batch, result):
            if klass not in valid_class_ids:
                log(f"  WARN: invalid class '{klass}' for {item['dispatch_id']} - using 'conversation' fallback")
                klass = "conversation"
                invalid_count += 1
            
            db.execute(
                "UPDATE dispatches SET task_class = ? WHERE dispatch_id = ?",
                (klass, item['dispatch_id'])
            )
            classified_count += 1
            class_counts[klass] = class_counts.get(klass, 0) + 1
        
        db.commit()
        log(f"  classified {len(result)} items (running total: {classified_count})")

    # Summary
    log("")
    log("=== CLASSIFICATION SUMMARY ===")
    log(f"  Total classified: {classified_count}/{len(enriched)}")
    log(f"  Invalid (defaulted): {invalid_count}")
    log(f"  Total tokens: {total_in_tokens} in + {total_out_tokens} out")
    # Haiku 4.5 pricing: $1/$5 per 1M
    cost = (total_in_tokens / 1e6 * 1.0) + (total_out_tokens / 1e6 * 5.0)
    log(f"  Estimated cost: ${cost:.4f}")
    log("")
    log("=== Distribution ===")
    for klass, n in sorted(class_counts.items(), key=lambda x: -x[1]):
        log(f"  {klass:30}  {n}")

    db.close()

if __name__ == "__main__":
    main()
