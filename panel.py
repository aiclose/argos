"""Argos cost-vs-quality panel.

Populates panel_decisions with a per-task-class recommendation: for each class,
rank enabled routes by quality (seeded prior or observed rate) and by effective
cost, surface the Pareto-efficient set (not dominated on cost AND quality), and
recommend the best score-per-dollar route that clears the class floor. Free/cheap
routes first; this is the "what should handle each class" view.

Writes one panel_decisions row per run with a JSON recommendation across classes.
Shadow/advisory: it recommends, Andy decides (andy_decision column).
"""
from __future__ import annotations
import sqlite3, json, datetime
import cost as costmod
import route_priors

DB = "/home/andy/argos/argos.db"

def _quality(con, route, task_class, error_sensitivity, floor):
    mid = route["model_id"]
    if mid and task_class:
        r = con.execute("SELECT COUNT(*) n, AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) rate "
                        "FROM dispatches WHERE model_used=? AND task_class=? AND accepted IS NOT NULL",
                        (mid, task_class)).fetchone()
        if r and r[0] and r[0] >= 5 and r[1] is not None:
            return float(r[1]), "observed"
    return route_priors.seed_prior(mid, route["tool"], error_sensitivity), "seed"

def pareto_front(items):
    """items: list of dicts with 'eff' (cost, lower better) and 'q' (quality, higher better).
    Return those not dominated (no other item is both cheaper-or-equal and higher-or-equal)."""
    front = []
    for a in items:
        dominated = False
        for b in items:
            if b is a:
                continue
            if b["eff"] <= a["eff"] and b["q"] >= a["q"] and (b["eff"] < a["eff"] or b["q"] > a["q"]):
                dominated = True
                break
        if not dominated:
            front.append(a)
    return front

def build_panel(dry_run=False):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    classes = con.execute("SELECT class_id, default_quality_floor, default_error_sensitivity "
                          "FROM task_classes").fetchall()
    routes = con.execute("SELECT route_id, tool, model_id, cost_mode, enabled FROM routes WHERE enabled=1").fetchall()
    task = costmod.Task()
    recommendation = {}
    for c in classes:
        cls, floor, es = c["class_id"], (c["default_quality_floor"] or 0.7), c["default_error_sensitivity"]
        scored = []
        for r in routes:
            eff = costmod.effective_cost(r["route_id"], task, con)
            q, src = _quality(con, r, cls, es, floor)
            spd = q / (eff + 1e-6)  # score per dollar
            scored.append({"route": r["route_id"], "eff": round(eff,6), "q": q,
                           "spd": round(spd,1), "clears": q >= floor, "src": src,
                           "cost_mode": r["cost_mode"]})
        clearing = [s for s in scored if s["clears"]]
        pool = clearing if clearing else scored
        # recommended: cheapest clearing route (matches the router), plus the Pareto set
        pool_sorted = sorted(pool, key=lambda s: (s["eff"], -s["q"]))
        front = sorted(pareto_front(scored), key=lambda s: s["eff"])
        recommendation[cls] = {
            "floor": floor,
            "recommended": pool_sorted[0]["route"] if pool_sorted else None,
            "recommended_q": pool_sorted[0]["q"] if pool_sorted else None,
            "any_clears": bool(clearing),
            "pareto": [{"route": f["route"], "eff": f["eff"], "q": f["q"]} for f in front[:5]],
            "top_score_per_dollar": sorted(scored, key=lambda s: -s["spd"])[0]["route"],
        }
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    week = datetime.date.today().isoformat()
    note = f"Auto-generated cost-vs-quality panel across {len(classes)} task classes. Quality from observed rates where >=5 labels, else benchmark seed prior. Cheapest-clearing-floor recommended per class; Pareto set + score-per-dollar leader included."
    if not dry_run:
        con.execute("INSERT INTO panel_decisions (week_start, recommendation, consensus_score, andy_decision, applied_at, notes) "
                    "VALUES (?,?,?,?,?,?)",
                    (week, json.dumps(recommendation), None, None, None, note))
        con.commit()
    # print a digest
    print(f"panel across {len(classes)} classes ({'DRY' if dry_run else 'written'}):")
    for cls, rec in list(recommendation.items())[:8]:
        flag = "" if rec["any_clears"] else "  [NO cheap route clears floor]"
        print(f"  {cls:24s} floor={rec['floor']:.2f} -> {rec['recommended']}{flag}")
    con.close()

if __name__ == "__main__":
    import sys
    build_panel(dry_run=("--dry" in sys.argv))
