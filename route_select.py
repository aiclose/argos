"""Argos route-aware selection (predict-then-optimise over ROUTES).

Additive upgrade to the Phase-1 router. The existing /route endpoint picks the
cheapest model within a tier from model_prices. This module instead selects over
the `routes` table (so Go vs Zen vs OpenRouter for the same model are distinct
options), scores each route by effective_cost (the shadow-price module), and
picks the CHEAPEST route whose predicted success clears the task-class quality
floor. Still shadow-mode: it recommends and logs, it does not execute.

Honest about the data: with almost no labelled outcomes yet (quality_score is
mostly NULL), predicted success leans on the task-class floor; this gets sharper
automatically as the predictor calibrates on accumulated accept/reject labels.

Exposed as select_route(task) so router.py can call it from a /route-v2 endpoint.
"""
from __future__ import annotations
import sqlite3, json
from dataclasses import dataclass, field
from typing import Optional

import cost as costmod  # the effective_cost module built alongside this

DB_PATH = "/home/andy/argos/argos.db"


@dataclass
class RouteTask:
    tag: str = ""
    task_class: Optional[str] = None
    error_sensitivity: Optional[str] = None
    estimated_input_tokens: int = 4000
    estimated_output_tokens: int = 1500


@dataclass
class RoutePlan:
    selected_route: Optional[str]
    selected_model: Optional[str]
    cost_mode: Optional[str]
    effective_cost: float
    quality_floor: float
    predicted_success: float
    cleared_floor: bool
    fallback_chain: list = field(default_factory=list)
    candidate_count: int = 0
    rationale: str = ""


def _conn():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _quality_floor(con, task: RouteTask) -> float:
    """Floor from task_classes.default_quality_floor; fall back to error_sensitivity."""
    if task.task_class:
        row = con.execute(
            "SELECT default_quality_floor FROM task_classes WHERE class_id=?",
            (task.task_class,)).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    # error-sensitivity fallback
    return {"low": 0.65, "medium": 0.70, "high": 0.85, "critical": 0.90}.get(
        task.error_sensitivity or "medium", 0.70)


def _predicted_success(con, route, task: RouteTask, floor: float) -> float:
    """Use logged outcomes for this route+task_class if we have enough; else floor.

    Deliberately simple per the research: do NOT pretend to a calibrated model
    until labels exist. If this route has >= MIN_OBS labelled dispatches for this
    task_class, use the observed accept rate; otherwise return the floor (neutral:
    the route is assumed just-good-enough, so selection falls to cost).
    """
    MIN_OBS = 5
    mid = route["model_id"]
    if not mid or not task.task_class:
        return floor
    row = con.execute(
        "SELECT COUNT(*) n, AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) rate "
        "FROM dispatches WHERE model_used=? AND task_class=? AND accepted IS NOT NULL",
        (mid, task.task_class)).fetchone()
    if row and row["n"] and row["n"] >= MIN_OBS and row["rate"] is not None:
        return float(row["rate"])
    return floor  # not enough evidence: neutral, let cost decide


def select_route(task: RouteTask) -> RoutePlan:
    con = _conn()
    try:
        floor = _quality_floor(con, task)
        routes = con.execute(
            "SELECT route_id, backend, tool, access_path, model_id, cost_mode, enabled "
            "FROM routes WHERE enabled=1").fetchall()
        ctask = costmod.Task(est_input_tokens=task.estimated_input_tokens,
                             est_output_tokens=task.estimated_output_tokens,
                             task_class=task.task_class)
        scored = []
        for r in routes:
            eff = costmod.effective_cost(r["route_id"], ctask, con)
            psucc = _predicted_success(con, r, task, floor)
            scored.append({
                "route_id": r["route_id"], "model_id": r["model_id"],
                "cost_mode": r["cost_mode"], "eff": eff,
                "psucc": psucc, "clears": psucc >= floor,
            })
        # predict-then-optimise: among routes that clear the floor, cheapest wins.
        clearing = [s for s in scored if s["clears"]]
        pool = clearing if clearing else scored  # if none clear, fall back to all
        pool.sort(key=lambda s: (s["eff"], -s["psucc"]))
        best = pool[0] if pool else None
        fallbacks = [s["route_id"] for s in pool[1:5]]

        if best is None:
            return RoutePlan(None, None, None, 0.0, floor, 0.0, False, [], 0,
                             "no enabled routes")

        used_fallback = not clearing
        rationale = (
            f"task_class={task.task_class} | floor={floor:.2f} | "
            f"strategy=cheapest-route-clearing-floor"
            + (" [NO route cleared floor, took cheapest overall]" if used_fallback else "")
            + f" | candidates={len(scored)} clearing={len(clearing)}"
            + f" | picked {best['route_id']} eff=${best['eff']:.6f} psucc={best['psucc']:.2f}"
        )
        return RoutePlan(
            selected_route=best["route_id"], selected_model=best["model_id"],
            cost_mode=best["cost_mode"], effective_cost=round(best["eff"], 6),
            quality_floor=floor, predicted_success=round(best["psucc"], 3),
            cleared_floor=not used_fallback, fallback_chain=fallbacks,
            candidate_count=len(scored), rationale=rationale,
        )
    finally:
        con.close()


def demo():
    """Show the plan for a few representative task types."""
    cases = [
        RouteTask(tag="t1", task_class="documentation", error_sensitivity="low"),
        RouteTask(tag="t2", task_class="code_generation", error_sensitivity="high"),
        RouteTask(tag="t3", task_class="test_unit", error_sensitivity="low"),
        RouteTask(tag="t4", task_class="code_algorithmic", error_sensitivity="high",
                  estimated_input_tokens=12000, estimated_output_tokens=4000),
    ]
    for c in cases:
        p = select_route(c)
        print(f"\n[{c.task_class} / {c.error_sensitivity}]")
        print(f"  -> {p.selected_route}  (${p.effective_cost}, {p.cost_mode})")
        print(f"     floor={p.quality_floor} psucc={p.predicted_success} cleared={p.cleared_floor}")
        print(f"     fallbacks: {p.fallback_chain[:3]}")


if __name__ == "__main__":
    demo()
