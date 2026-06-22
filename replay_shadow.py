#!/usr/bin/env python3
"""Argos shadow REPLAY - the go-live evidence table (ARGOS-S2 2b / CHG-P9-049).

Runs ON GARAGE, where argos.db is local. For every historical dispatch that has
a task_class + a realised model + a realised cost, this asks "what would Argos
/route-v2 pick NOW?" by calling route_select.select_route IN-PROCESS (no HTTP, no
service dependency, and faster), then compares the realised model + cost against
Argos's route-aware effective_cost.

Why a replay is the PRIMARY evidence: live shadow volume is thin (~193 dispatches
to date). The historical replay - not live volume - is therefore the main go-live
evidence: it shows, per task_class and overall, the cost delta if Argos had been
steering all along.

Honest by construction:
  * Realised cost comes from dispatches.actual_cost_usd, which dispatch_tail
    already denormalises from cost_log at ingest time. cost_log itself lives on
    UM780 (not in argos.db), so no fragile cross-host join is needed or attempted;
    the costlog-<id> dispatch_id only records provenance. Rows with no realised
    cost are SKIPPED and counted, never guessed.
  * A single bad or erroring row is skipped + counted; it never aborts the replay.
  * Argos's effective_cost is the route-aware shadow price. It is directly
    comparable as a cost steer; it uses each dispatch's realised token counts when
    present, else route_select's own defaults. This is analysis, not a migration.

Output: a full per-dispatch table plus per-task-class and overall aggregates to
stdout, and (best-effort) the same markdown to the Aegis vault at
  _Homelab/canonical/argos-shadow-replay-2026-06-22.md
when AEGIS_API_URL is set; otherwise stdout only and the operator captures it.
All generated markdown uses hyphens, not emdashes.

Usage:
  python3 replay_shadow.py [--limit N] [--db /path/to/argos.db]
Programmatic:
  from replay_shadow import build_replay
  result = build_replay(limit=5)   # result["rows"], result["summary"], ...
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import route_select
from route_select import RouteTask, select_route

VAULT_PATH = "_Homelab/canonical/argos-shadow-replay-2026-06-22.md"
REPORT_DATE = "2026-06-22"

# Columns we read from dispatches when present (resolved dynamically so a schema
# drift never crashes the replay).
_OPTIONAL_DISPATCH_COLS = (
    "error_sensitivity", "actual_input_tokens", "actual_output_tokens",
)


def _ro_conn(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _dispatch_columns(con):
    return {row[1] for row in con.execute("PRAGMA table_info(dispatches)")}


def _num(v):
    """Coerce to float or None (never raise)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pos_int(v):
    """Coerce to a positive int or None."""
    try:
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def build_replay(db_path=None, limit=None):
    """Replay every eligible historical dispatch through select_route in-process.

    Returns a dict:
      {
        "rows":   [ {dispatch_id, task_class, realised_model, realised_cost,
                     argos_model, argos_effective_cost, delta_cost, would_switch,
                     note}, ... ],
        "skipped": int,
        "skip_reasons": {reason: count},
        "argos_errors": int,
        "by_class": { task_class: {n, switches, realised_cost, argos_cost,
                                   delta_cost, pct_saved} },
        "summary": {n, switches, realised_cost, argos_cost, delta_cost,
                    pct_saved, skipped, argos_errors},
        "db_path": str,
      }

    Robust: any per-row failure is caught, counted, and skipped - it never aborts.
    """
    db_path = db_path or route_select.DB_PATH

    rows = []
    skipped = 0
    skip_reasons = {}
    argos_errors = 0

    def _skip(reason):
        nonlocal skipped
        skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    con = _ro_conn(db_path)
    try:
        cols = _dispatch_columns(con)
        present = [c for c in _OPTIONAL_DISPATCH_COLS if c in cols]
        select_cols = ["dispatch_id", "task_class", "model_used", "actual_cost_usd"] + present
        sql = f"SELECT {', '.join(select_cols)} FROM dispatches ORDER BY dispatch_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        con.row_factory = sqlite3.Row
        dispatch_rows = con.execute(sql).fetchall()
    finally:
        con.close()

    for d in dispatch_rows:
        try:
            d = dict(d)
            task_class = d.get("task_class")
            realised_model = d.get("model_used")
            realised_cost = _num(d.get("actual_cost_usd"))

            if not task_class:
                _skip("no_task_class")
                continue
            if not realised_model:
                _skip("no_realised_model")
                continue
            if realised_cost is None:
                _skip("no_realised_cost")
                continue

            es = d.get("error_sensitivity") or "medium"
            kwargs = dict(
                tag=d.get("dispatch_id") or "",
                task_class=task_class,
                error_sensitivity=es,
            )
            tin = _pos_int(d.get("actual_input_tokens"))
            tout = _pos_int(d.get("actual_output_tokens"))
            if tin is not None:
                kwargs["estimated_input_tokens"] = tin
            if tout is not None:
                kwargs["estimated_output_tokens"] = tout

            argos_model = None
            argos_cost = None
            note = ""
            try:
                plan = select_route(RouteTask(**kwargs))
                if plan.error or not (plan.selected_model or plan.selected_route):
                    argos_errors += 1
                    note = f"argos_no_route: {plan.error}" if plan.error else "argos_no_route"
                else:
                    argos_model = plan.selected_model or plan.selected_route
                    argos_cost = _num(plan.effective_cost)
            except Exception as e:  # select_route blew up on this row - record, do not abort
                argos_errors += 1
                note = f"argos_error: {type(e).__name__}: {e}"

            delta_cost = (realised_cost - argos_cost) if argos_cost is not None else None
            would_switch = bool(argos_model is not None and argos_model != realised_model)

            rows.append({
                "dispatch_id": d.get("dispatch_id"),
                "task_class": task_class,
                "realised_model": realised_model,
                "realised_cost": realised_cost,
                "argos_model": argos_model,
                "argos_effective_cost": argos_cost,
                "delta_cost": delta_cost,
                "would_switch": would_switch,
                "note": note,
            })
        except Exception as e:  # truly defensive: one bad row must not kill the replay
            _skip(f"row_error:{type(e).__name__}")
            continue

    by_class = _aggregate_by_class(rows)
    summary = _summary(rows, by_class, skipped, argos_errors)

    return {
        "rows": rows,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "argos_errors": argos_errors,
        "by_class": by_class,
        "summary": summary,
        "db_path": db_path,
    }


def _pct_saved(realised, argos):
    """Percent of realised spend saved if Argos had steered. Positive = cheaper."""
    if not realised:
        return None
    return (realised - argos) / realised * 100.0


def _aggregate_by_class(rows):
    agg = {}
    for r in rows:
        tc = r["task_class"]
        a = agg.setdefault(tc, {"n": 0, "switches": 0, "realised_cost": 0.0,
                                "argos_cost": 0.0, "delta_cost": 0.0, "_costed": 0})
        a["n"] += 1
        if r["would_switch"]:
            a["switches"] += 1
        # Only rows with BOTH costs contribute to the cost deltas (honest sums).
        if r["realised_cost"] is not None and r["argos_effective_cost"] is not None:
            a["realised_cost"] += r["realised_cost"]
            a["argos_cost"] += r["argos_effective_cost"]
            a["delta_cost"] += r["delta_cost"]
            a["_costed"] += 1
    for tc, a in agg.items():
        a["pct_saved"] = _pct_saved(a["realised_cost"], a["argos_cost"])
    return agg


def _summary(rows, by_class, skipped, argos_errors):
    n = len(rows)
    switches = sum(1 for r in rows if r["would_switch"])
    costed = [r for r in rows if r["realised_cost"] is not None and r["argos_effective_cost"] is not None]
    realised = sum(r["realised_cost"] for r in costed)
    argos = sum(r["argos_effective_cost"] for r in costed)
    delta = sum(r["delta_cost"] for r in costed)
    return {
        "n": n,
        "switches": switches,
        "costed_rows": len(costed),
        "realised_cost": realised,
        "argos_cost": argos,
        "delta_cost": delta,
        "pct_saved": _pct_saved(realised, argos),
        "skipped": skipped,
        "argos_errors": argos_errors,
    }


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

def _fmt_cost(v):
    return "n/a" if v is None else f"${v:.6f}"


def _fmt_pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def render_markdown(result):
    """Render the full evidence table + aggregates as markdown (hyphens only)."""
    s = result["summary"]
    out = []
    out.append(f"# Argos shadow replay - go-live evidence ({REPORT_DATE})")
    out.append("")
    out.append("Replay of historical dispatches through Argos route_select.select_route "
               "(the /route-v2 selection path) IN-PROCESS. For each dispatch: what "
               "actually ran vs what Argos would pick now, with the cost delta. "
               "delta_cost = realised_cost - argos_effective_cost (positive = Argos cheaper). "
               "This historical replay is the primary go-live evidence; live shadow "
               "volume is still thin.")
    out.append("")
    out.append("## Headline")
    out.append("")
    headline = _fmt_pct(s["pct_saved"])
    out.append(f"- Cost delta if Argos had been steering: **{headline}** of realised spend "
               f"(over {s['costed_rows']} cost-comparable dispatches).")
    out.append(f"- Realised spend: {_fmt_cost(s['realised_cost'])} | "
               f"Argos shadow spend: {_fmt_cost(s['argos_cost'])} | "
               f"delta: {_fmt_cost(s['delta_cost'])}.")
    out.append(f"- Would switch model on {s['switches']} / {s['n']} replayed dispatches.")
    out.append(f"- Skipped (no task_class / no realised model / no realised cost): {s['skipped']}.")
    out.append(f"- Argos no-route / error rows (counted, cost-excluded): {s['argos_errors']}.")
    if result["skip_reasons"]:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(result["skip_reasons"].items()))
        out.append(f"- Skip reasons: {reasons}.")
    out.append("")
    out.append("## Per task_class")
    out.append("")
    out.append("| task_class | N | would_switch | realised_cost | argos_cost | delta | % saved |")
    out.append("|---|---|---|---|---|---|---|")
    for tc in sorted(result["by_class"]):
        a = result["by_class"][tc]
        out.append(f"| {tc} | {a['n']} | {a['switches']} | {_fmt_cost(a['realised_cost'])} | "
                   f"{_fmt_cost(a['argos_cost'])} | {_fmt_cost(a['delta_cost'])} | "
                   f"{_fmt_pct(a['pct_saved'])} |")
    out.append(f"| **TOTAL** | {s['n']} | {s['switches']} | {_fmt_cost(s['realised_cost'])} | "
               f"{_fmt_cost(s['argos_cost'])} | {_fmt_cost(s['delta_cost'])} | "
               f"{_fmt_pct(s['pct_saved'])} |")
    out.append("")
    out.append("## Per dispatch")
    out.append("")
    out.append("| dispatch_id | task_class | realised_model | realised_cost | argos_model | argos_cost | delta | switch | note |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in result["rows"]:
        out.append(
            f"| {r['dispatch_id']} | {r['task_class']} | {r['realised_model']} | "
            f"{_fmt_cost(r['realised_cost'])} | {r['argos_model'] or 'n/a'} | "
            f"{_fmt_cost(r['argos_effective_cost'])} | {_fmt_cost(r['delta_cost'])} | "
            f"{'yes' if r['would_switch'] else 'no'} | {r['note'] or ''} |"
        )
    out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Vault write (best-effort)
# ----------------------------------------------------------------------------

def write_vault(content, vault_path=VAULT_PATH):
    """Best-effort write of the markdown to the Aegis vault.

    Only attempts when AEGIS_API_URL is set; on any failure (or if unset) it
    returns a short status string and the operator captures stdout instead.
    Never raises.
    """
    api = os.environ.get("AEGIS_API_URL")
    if not api:
        return "vault: skipped (AEGIS_API_URL unset; capture stdout)"
    import urllib.request
    import urllib.error
    try:
        body = json.dumps({"path": vault_path, "content": content}).encode()
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("AEGIS_API_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(api.rstrip("/") + "/vault/write",
                                     data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return f"vault: wrote {vault_path} via {api}"
    except Exception as e:
        return f"vault: FAILED ({type(e).__name__}: {e}); capture stdout instead"


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    limit = None
    db_path = None
    i = 0
    while i < len(argv):
        if argv[i] == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 2
        elif argv[i] == "--db" and i + 1 < len(argv):
            db_path = argv[i + 1]; i += 2
        else:
            i += 1

    result = build_replay(db_path=db_path, limit=limit)
    md = render_markdown(result)
    print(md)

    # Local capture copy (always harmless) + best-effort vault write.
    local = f"argos-shadow-replay-{REPORT_DATE}.md"
    try:
        with open(local, "w") as f:
            f.write(md + "\n")
        print(f"\n[local copy written: {os.path.abspath(local)}]")
    except Exception as e:
        print(f"\n[local copy failed: {e}]")
    print(f"[{write_vault(md)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
