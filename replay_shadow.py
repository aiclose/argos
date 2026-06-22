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
  python3 replay_shadow.py [--limit N] [--db /path/to/argos.db] [--codex-cap N]
    --codex-cap N : treat codex-oauth's daily_requests_cap as N for this run only
                    (sensitivity sweep). Default: the stored quota_caps value.
Programmatic:
  from replay_shadow import build_replay
  result = build_replay(limit=5)   # result["rows"], result["summary"], ...
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import cost as costmod
import route_select
from route_select import RouteTask, select_route

VAULT_PATH = "_Homelab/canonical/argos-shadow-replay-2026-06-22.md"
REPORT_DATE = "2026-06-22"

# CHG-P9-050: the flat-rate codex bucket. Its notional daily_requests_cap (default
# 200, maintained by argos_price_quota_pull.py) is what makes the bucket shadow
# price ramp; --codex-cap overrides it for a single run. token-estimate fallbacks
# match route_select.RouteTask defaults so the ledger counts the same tokens the
# cost model priced.
CODEX_BUCKET = "codex-oauth"
_FALLBACK_EST_INPUT = 4000
_FALLBACK_EST_OUTPUT = 1500

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


def _day_of(ts):
    """Calendar day key from a dispatch timestamp. ISO 'YYYY-MM-DD[ T]HH:MM:SS' ->
    its first 10 chars (the calendar day, same basis the timestamps are stored in).
    None/blank -> 'unknown' so undated rows still accumulate into one bucket-day."""
    if ts is None:
        return "unknown"
    s = str(ts)
    return s[:10] if len(s) >= 10 else (s or "unknown")


def _replay_metadata(con):
    """One-shot reads that drive the usage ledger + honesty reporting:
      - KNOWN_BUCKETS: distinct non-null routes.quota_bucket (codex-oauth always in).
      - route_bucket:  route_id -> its quota_bucket (to attribute usage to a bucket).
      - codex:         the enabled+healthy codex route/model (for spill detection).
      - caps:          bucket -> stored daily_requests_cap (for peak-vs-cap reporting).
    Each read is guarded so a schema variant (e.g. a fixture without quota_caps)
    degrades to empty rather than aborting the replay."""
    route_bucket = {}
    known = set()
    try:
        for rid, b in con.execute(
                "SELECT route_id, quota_bucket FROM routes WHERE quota_bucket IS NOT NULL"):
            route_bucket[rid] = b
            known.add(b)
    except sqlite3.Error:
        pass
    known.add(CODEX_BUCKET)  # always price the codex ration even if no route maps yet

    codex = None
    try:
        for rid, mid, en, ht, lh in con.execute(
                "SELECT route_id, model_id, enabled, healthcheck_type, last_health "
                "FROM routes WHERE quota_bucket=?", (CODEX_BUCKET,)):
            if en and ht in ("cli-smoke", "api-chat") and lh == "ok":
                codex = {"route_id": rid, "model_id": mid}
                break
    except sqlite3.Error:
        pass

    caps = {}
    try:
        for b, rc in con.execute("SELECT bucket, daily_requests_cap FROM quota_caps"):
            if rc is not None:
                caps[b] = rc
    except sqlite3.Error:
        pass
    return sorted(known), route_bucket, codex, caps


def build_replay(db_path=None, limit=None, codex_cap=None):
    """Replay every eligible historical dispatch through select_route in-process,
    accumulating a per-(bucket, day) usage ledger so capped buckets (codex-oauth)
    price convexly as their daily ration fills - exactly as the live cost model
    would over a real day. CHG-P9-050.

    Rows are walked CHRONOLOGICALLY (dispatches.ts ascending) so usage accumulates
    in time order. Before each dispatch is costed, the day's ledger snapshot is
    injected into the cost model via cost._BUCKET_USAGE_OVERRIDE (process-local,
    reset after every row); after costing, the chosen route's bucket usage is
    incremented. The live router path never sets that override and is unaffected.

    codex_cap: if not None, codex-oauth's daily_requests_cap is treated as this
    value for THIS run only (via cost._BUCKET_CAP_OVERRIDE); else the stored
    quota_caps value is used. Lets the operator sweep the ration with no code edit.

    Returns the prior dict plus honesty fields: peak_usage {bucket: {peak, day, cap}},
    spills (int), spill_dispatch_ids (list), verdict (str), codex_cap_used.

    Robust: any per-row failure is caught, counted, and skipped - it never aborts.
    """
    db_path = db_path or route_select.DB_PATH

    rows = []
    skipped = 0
    skip_reasons = {}
    argos_errors = 0
    ledger = {}                 # (bucket, day) -> [requests, tokens, cost]
    peak_usage = {}             # bucket -> {"peak": req, "day": day}
    spill_ids = []              # codeable dispatches that spilled off codex post-ramp

    def _skip(reason):
        nonlocal skipped
        skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    con = _ro_conn(db_path)
    try:
        con.row_factory = sqlite3.Row
        cols = _dispatch_columns(con)
        present = [c for c in _OPTIONAL_DISPATCH_COLS if c in cols]
        ts_col = "ts" if "ts" in cols else ("created_at" if "created_at" in cols else None)
        select_cols = ["dispatch_id", "task_class", "model_used", "actual_cost_usd"] + present
        if ts_col:
            select_cols.append(ts_col)
        order_col = ts_col or "dispatch_id"   # chronological when ts exists; stable otherwise
        sql = f"SELECT {', '.join(select_cols)} FROM dispatches ORDER BY {order_col} ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        dispatch_rows = con.execute(sql).fetchall()
        known_buckets, route_bucket, codex, caps = _replay_metadata(con)
    finally:
        con.close()

    # Effective codex ration for THIS run: --codex-cap override wins over stored cap.
    codex_cap_used = codex_cap if codex_cap is not None else caps.get(CODEX_BUCKET)
    eff_caps = dict(caps)
    if codex_cap is not None:
        eff_caps[CODEX_BUCKET] = codex_cap
    # Process-local cap override (None in production); set once for the whole run.
    if codex_cap is not None:
        costmod.set_bucket_cap_override({CODEX_BUCKET: codex_cap})

    def _bump_peak(bucket, day, req):
        cur = peak_usage.get(bucket)
        if cur is None or req > cur["peak"]:
            peak_usage[bucket] = {"peak": req, "day": day}

    def _in_buffer_band(bucket, used_req):
        """True when used_req has entered the convex band of its effective cap
        (R = cap - used < BUFFER_FRAC*cap) - i.e. the codex price has begun to ramp."""
        cap = eff_caps.get(bucket)
        if not cap or cap <= 0:
            return False
        return (cap - used_req) < costmod.BUFFER_FRAC * cap

    try:
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

                day = _day_of(d.get(ts_col) if ts_col else None)
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
                est_in = tin if tin is not None else _FALLBACK_EST_INPUT
                est_out = tout if tout is not None else _FALLBACK_EST_OUTPUT

                # codex usage BEFORE this dispatch (was its price already ramping?).
                codex_used_before = ledger.get((CODEX_BUCKET, day), [0, 0, 0.0])[0]

                argos_model = None
                argos_route = None
                argos_cost = None
                note = ""
                try:
                    # Inject the day's usage snapshot for every known bucket, then cost.
                    costmod.set_bucket_usage_override(
                        {b: tuple(ledger.get((b, day), [0, 0, 0.0])) for b in known_buckets})
                    plan = select_route(RouteTask(**kwargs))
                    if plan.error or not (plan.selected_model or plan.selected_route):
                        argos_errors += 1
                        note = f"argos_no_route: {plan.error}" if plan.error else "argos_no_route"
                    else:
                        argos_model = plan.selected_model or plan.selected_route
                        argos_route = plan.selected_route
                        argos_cost = _num(plan.effective_cost)
                except Exception as e:  # select_route blew up on this row - record, do not abort
                    argos_errors += 1
                    note = f"argos_error: {type(e).__name__}: {e}"
                finally:
                    costmod.set_bucket_usage_override(None)  # never leak the override

                # Attribute this dispatch's usage to the chosen route's bucket (if any),
                # so the next same-day dispatch sees a fuller ledger and prices higher.
                chosen_bucket = route_bucket.get(argos_route) if argos_route else None
                if chosen_bucket:
                    cell = ledger.setdefault((chosen_bucket, day), [0, 0, 0.0])
                    cell[0] += 1
                    cell[1] += (est_in + est_out)
                    cell[2] += (argos_cost or 0.0)
                    _bump_peak(chosen_bucket, day, cell[0])

                # Spill proxy: a codeable dispatch where codex was a healthy candidate,
                # its ration had already entered the buffer band that day, yet the pick
                # went elsewhere - i.e. the ramp pushed work off the subscription.
                chose_codex = bool(codex and argos_route == codex["route_id"])
                if (codex and not chose_codex and argos_route is not None
                        and _in_buffer_band(CODEX_BUCKET, codex_used_before)):
                    spill_ids.append(d.get("dispatch_id"))

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
    finally:
        costmod.set_bucket_usage_override(None)  # belt-and-braces: no leak on any exit
        costmod.set_bucket_cap_override(None)

    # Attach the effective cap to each peak record for the honesty table.
    for b, rec in peak_usage.items():
        rec["cap"] = eff_caps.get(b)

    by_class = _aggregate_by_class(rows)
    summary = _summary(rows, by_class, skipped, argos_errors)
    spills = len(spill_ids)
    summary["spills"] = spills
    verdict = _verdict(spills, peak_usage, codex_cap_used)

    return {
        "rows": rows,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "argos_errors": argos_errors,
        "by_class": by_class,
        "summary": summary,
        "peak_usage": peak_usage,
        "spills": spills,
        "spill_dispatch_ids": spill_ids,
        "codex_cap_used": codex_cap_used,
        "verdict": verdict,
        "db_path": db_path,
    }


def _verdict(spills, peak_usage, codex_cap_used):
    """Computed, honest one-liner on whether the codex ration actually bound."""
    peak_rec = peak_usage.get(CODEX_BUCKET)
    peak = peak_rec["peak"] if peak_rec else 0
    cap = codex_cap_used
    if spills > 0:
        return (f"codex cap bound: {spills} codeable dispatch(es) spilled to per-token "
                f"after codex hit its daily ration (peak {peak} req/day vs cap {cap}); "
                f"headline saving is capacity-bounded.")
    cap_str = cap if cap is not None else "n/a"
    return (f"codex cap did NOT bind at replayed volume (peak {peak} req/day << cap "
            f"{cap_str}). The saving reflects genuine spare flat-rate capacity at "
            f"current volume, not a pricing phantom; it would erode as daily codeable "
            f"volume approaches the cap.")


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
    # CHG-P9-050: capacity-honesty - did the codex ration actually bind?
    out.append("## Capacity honesty (did the codex ration bind?)")
    out.append("")
    cap_used = result.get("codex_cap_used")
    out.append(f"- codex-oauth daily_requests_cap in force this run: "
               f"**{cap_used if cap_used is not None else 'none (uncapped)'}**.")
    peak_usage = result.get("peak_usage") or {}
    if peak_usage:
        out.append("- Peak single-day request usage vs cap:")
        for b in sorted(peak_usage):
            rec = peak_usage[b]
            capstr = rec.get("cap")
            capstr = capstr if capstr is not None else "no cap"
            out.append(f"  - {b}: peak {rec['peak']} req/day (on {rec['day']}) vs cap {capstr}.")
    else:
        out.append("- Peak single-day request usage: no bucketed routes were selected.")
    out.append(f"- Spill count (codeable dispatches pushed off codex after its ration ramped): "
               f"**{result.get('spills', 0)}**.")
    out.append(f"- VERDICT: {result.get('verdict', 'n/a')}")
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
    codex_cap = None
    i = 0
    while i < len(argv):
        if argv[i] == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 2
        elif argv[i] == "--db" and i + 1 < len(argv):
            db_path = argv[i + 1]; i += 2
        elif argv[i] == "--codex-cap" and i + 1 < len(argv):
            codex_cap = int(argv[i + 1]); i += 2
        else:
            i += 1

    result = build_replay(db_path=db_path, limit=limit, codex_cap=codex_cap)
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
