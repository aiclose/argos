#!/usr/bin/env python3
"""BENCH-001: Fold established AI benchmarks into argos eval_tasks.json.

Fetches MMLU-Pro, original MMLU, and HumanEval via the HuggingFace
datasets-server REST API (stdlib urllib only -- no `datasets`, no `requests`),
transforms each row into the existing eval_tasks.json schema, and APPENDS them
idempotently (skipping ids that already exist).

SHADOW / eval-only. Does not touch any service code.

Usage:
    python3 fetch_benchmarks.py            # real fetch + write
    python3 fetch_benchmarks.py --dry-run  # print what would be added, no write
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_TASKS_PATH = os.path.join(HERE, "eval_tasks.json")

DSS_BASE = "https://datasets-server.huggingface.co/rows"

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only)
# ---------------------------------------------------------------------------
def fetch_rows(dataset, config, split, offset=0, length=100, retries=3):
    """Fetch a page of rows from the datasets-server. Returns list of row dicts.

    length is capped at 100 by the API. Raises on persistent HTTP failure.
    """
    length = min(length, 100)
    params = urllib.parse.urlencode({
        "dataset": dataset,
        "config": config,
        "split": split,
        "offset": offset,
        "length": length,
    })
    url = f"{DSS_BASE}?{params}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "argos-bench/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return [r["row"] for r in payload.get("rows", [])]
        except urllib.error.HTTPError as e:
            last_err = e
            # Back off harder on rate limiting; honor Retry-After if present.
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else 5.0 * (attempt + 1)
                except ValueError:
                    wait = 5.0 * (attempt + 1)
                time.sleep(min(wait, 30.0))
            else:
                time.sleep(2.0 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2.0 * (attempt + 1))
    raise last_err


def render_options(options):
    """Render a list of option strings as 'A) ... B) ...' lines."""
    lines = []
    for i, opt in enumerate(options):
        lines.append(f"{LETTERS[i]}) {opt}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-source transforms -> eval_tasks schema
# ---------------------------------------------------------------------------
MCQ_INSTRUCTION = "Answer with the single correct option letter and a one-line justification."
MCQ_RUBRIC = "correct-answer:0.8 reasoning:0.2"


def build_mmlu_pro(target=15):
    """MMLU-Pro: spread across distinct row 'category' values for diversity."""
    # MMLU-Pro test is ordered by category, so fetch pages at spread offsets
    # across the ~12k rows to capture many distinct categories, then sample.
    rows = []
    for offset in (0, 1500, 3000, 4500, 6000, 7500, 9000, 10500, 11500):
        try:
            page = fetch_rows("TIGER-Lab/MMLU-Pro", "default", "test", offset=offset, length=100)
        except Exception as e:
            print(f"  [mmlu-pro] page offset={offset} failed: {e}", file=sys.stderr)
            page = []
        if not page:
            break
        rows.extend(page)

    # Group by row category.
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "unknown"), []).append(r)

    # Round-robin across categories for diversity.
    tasks = []
    cats = sorted(by_cat.keys())
    idxs = {c: 0 for c in cats}
    while len(tasks) < target and cats:
        progressed = False
        for c in list(cats):
            if len(tasks) >= target:
                break
            bucket = by_cat[c]
            if idxs[c] >= len(bucket):
                continue
            r = bucket[idxs[c]]
            idxs[c] += 1
            progressed = True
            options = r.get("options") or []
            ai = r.get("answer_index")
            if not options or ai is None or ai >= len(options):
                continue
            qid = r.get("question_id", r.get("row_idx", len(tasks)))
            correct_letter = LETTERS[ai]
            correct_text = options[ai]
            prompt = (
                f"{r['question']}\n\n{render_options(options)}\n\n{MCQ_INSTRUCTION}"
            )
            tasks.append({
                "id": f"mmlu-pro/{qid}",
                "category": "reasoning",
                "prompt": prompt,
                "expected": f"{correct_letter}) {correct_text}",
                "rubric": MCQ_RUBRIC,
            })
        if not progressed:
            break
    return tasks


def build_mmlu(target=15):
    """Original MMLU: spread across several distinct 'subject' values."""
    # cais/mmlu 'all' test is grouped by subject alphabetically, so fetch a few
    # pages at spread offsets to collect distinct subjects.
    rows = []
    for offset in (0, 1500, 3000, 5000, 7000, 9000, 11000, 13000):
        try:
            page = fetch_rows("cais/mmlu", "all", "test", offset=offset, length=100)
        except Exception as e:
            print(f"  [mmlu] page offset={offset} failed: {e}", file=sys.stderr)
            page = []
        rows.extend(page)

    by_subject = {}
    for i, r in enumerate(rows):
        by_subject.setdefault(r.get("subject", "unknown"), []).append((i, r))

    tasks = []
    subjects = sorted(by_subject.keys())
    idxs = {s: 0 for s in subjects}
    while len(tasks) < target and subjects:
        progressed = False
        for s in list(subjects):
            if len(tasks) >= target:
                break
            bucket = by_subject[s]
            if idxs[s] >= len(bucket):
                continue
            i, r = bucket[idxs[s]]
            idxs[s] += 1
            progressed = True
            choices = r.get("choices") or []
            ans = r.get("answer")
            if not choices or ans is None or ans >= len(choices):
                continue
            correct_letter = LETTERS[ans]
            correct_text = choices[ans]
            prompt = (
                f"{r['question']}\n\n{render_options(choices)}\n\n{MCQ_INSTRUCTION}"
            )
            tasks.append({
                "id": f"mmlu/{s}/{i}",
                "category": "reasoning",
                "prompt": prompt,
                "expected": f"{correct_letter}) {correct_text}",
                "rubric": MCQ_RUBRIC,
            })
        if not progressed:
            break
    return tasks


def build_humaneval(target=15):
    """HumanEval: first N are fine (they are ordered)."""
    try:
        rows = fetch_rows("openai/openai_humaneval", "openai_humaneval", "test",
                          offset=0, length=min(target, 100))
    except Exception as e:
        print(f"  [humaneval] fetch failed: {e}", file=sys.stderr)
        return []
    tasks = []
    for r in rows[:target]:
        task_id = r.get("task_id", "")
        prompt = r.get("prompt", "")
        solution = r.get("canonical_solution", "")
        if not prompt or not solution:
            continue
        tasks.append({
            "id": f"humaneval/{task_id}",
            "category": "code_generation",
            "prompt": f"{prompt}\nComplete the function.",
            "expected": solution,
            "rubric": "code-correctness:0.7 edge-cases:0.2 style:0.1",
        })
    return tasks


SOURCES = [
    ("mmlu-pro", build_mmlu_pro),
    ("mmlu", build_mmlu),
    ("humaneval", build_humaneval),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv[1:]

    with open(EVAL_TASKS_PATH, "r", encoding="utf-8") as f:
        existing = json.load(f)
    assert isinstance(existing, list), "eval_tasks.json must be a JSON array"
    existing_ids = {t["id"] for t in existing}
    orig_total = len(existing)

    per_source = {}
    new_tasks = []
    seen_new = set()
    for name, builder in SOURCES:
        try:
            built = builder()
        except Exception as e:
            print(f"[{name}] FAILED, continuing: {e}", file=sys.stderr)
            per_source[name] = 0
            continue
        added = 0
        for t in built:
            if t["id"] in existing_ids or t["id"] in seen_new:
                continue
            seen_new.add(t["id"])
            new_tasks.append(t)
            added += 1
        per_source[name] = added

    new_total = orig_total + len(new_tasks)

    # No id collisions among the merged set.
    merged_ids = [t["id"] for t in existing] + [t["id"] for t in new_tasks]
    assert len(merged_ids) == len(set(merged_ids)), "id collision detected!"

    print("=" * 60)
    print("BENCH-001 fetch summary" + ("  [DRY RUN]" if dry_run else ""))
    print("=" * 60)
    for name, _ in SOURCES:
        print(f"  {name:12s}: +{per_source.get(name, 0)} tasks")
    print(f"  {'TOTAL ADDED':12s}: +{len(new_tasks)}")
    print(f"  existing total : {orig_total}")
    print(f"  new total      : {new_total}")
    print("=" * 60)

    if dry_run:
        print("\n[DRY RUN] would add these ids:")
        for t in new_tasks:
            print(f"  {t['id']}  ({t['category']})")
        print("\n[DRY RUN] no file written.")
        return

    if not new_tasks:
        print("Nothing new to add (all ids already present). File unchanged.")
        return

    merged = existing + new_tasks
    with open(EVAL_TASKS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=True)
        f.write("\n")
    print(f"Wrote {EVAL_TASKS_PATH} ({new_total} tasks).")


if __name__ == "__main__":
    main()
