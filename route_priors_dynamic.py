"""DB-driven quality priors for the shadow router (U4-001).

route_priors.py warm-starts each route from STALE published-benchmark numbers.
This module adds a *live* warm-start layer on top of those seeds, sourced from
two evidence tables the rest of Argos already maintains:

  A) panel_decisions  - the weekly 5-AI strategy panel (panel.py). Its
     `recommendation` JSON is keyed by the SAME 24-class underscored taxonomy
     the live router uses (code_generation, debugging, ...), and its route_id
     strings match routes.route_id. This is the CLEAN source and the primary
     prior here.
  B) champions         - the bake-off winner per class (weekly_eval_sprt.py).
     CAUTION: champions.task_class uses the bake-off taxonomy (hyphenated:
     code-debug, file-edit, ...), which does NOT line up 1:1 with the 24-class
     dispatches taxonomy. We only consult champions through an explicit,
     deliberately tiny CHAMPION_CLASS_MAP for mappings we can defend; anything
     ambiguous is skipped rather than guessed.

Design contract (enforced by callers, re-stated for safety):
  * SHADOW STAYS SHADOW. This only nudges the predicted-success PRIOR. It does
    not touch gates, the shadow flag, or live traffic.
  * Real observed accept-rate (tier-1, >=5 labels) always wins; the caller never
    asks us to boost over it. We only sharpen the cold-start guess.
  * The nudge is BOUNDED, BOOST-ONLY (never below base), and HARD-CAPPED at 0.95.
  * Best-effort: a malformed panel JSON or a missing table must NOT raise into
    route selection. Every public entry point degrades to "no boost".
"""
from __future__ import annotations
import json
import logging
import sqlite3
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Hard ceiling on any boosted prior. A warm-start guess should never claim more
# confidence than a near-certain route; real data has to earn anything above this.
HARD_CAP = 0.95
# Full adoption of recommended_q (via max) is the strongest nudge. The pareto
# nudge is deliberately partial so a non-recommended-but-on-the-frontier route
# moves less than the panel's explicit pick.
PARETO_BLEND = 0.5
# Fixed, small bump for the active bake-off champion of a mapped class. Smaller
# than a panel signal because champions reach us through a lossy taxonomy map.
CHAMPION_BOOST = 0.05

# --- Champion taxonomy bridge --------------------------------------------------
# Keyed bake-off class -> 24-class underscored class. ONLY mappings we can
# justify as a clean semantic 1:1 are listed. Each decision is documented; the
# rest are intentionally absent (we skip rather than mis-map).
#
#   code-debug    -> debugging       : both mean "fix broken code". Clean 1:1.
#   code-write-new-> code_generation : "write new code" == the generation class.
#
# Deliberately NOT mapped (ambiguous -> skipped, per the task's "do not guess"):
#   reasoning        -> no clean home in the 24 (analysis? architecture_design?
#                       conversation?). No defensible 1:1, so it is dropped.
#   file-edit        -> a file edit can be a fix, a feature, or a refactor.
#                       Maps to no single class without guessing. Dropped.
#   file-edit-single -> same ambiguity as file-edit, only narrower in scope.
#                       Dropped.
CHAMPION_CLASS_MAP = {
    "code-debug": "debugging",
    "code-write-new": "code_generation",
}


def _is_rejected(andy_decision: Optional[str]) -> bool:
    """True only when andy_decision EXPLICITLY rejects the panel.

    Per the verified ground truth, andy_decision is NULL for every panel row
    today, and NULL is treated as ADVISORY (usable as a soft warm-start prior).
    We therefore exclude a panel ONLY when the operator left an explicit
    rejection marker; we never gate the whole feature off an always-NULL column.
    """
    if andy_decision is None:
        return False
    token = str(andy_decision).strip().lower()
    return token in {"rejected", "reject", "vetoed", "veto", "no", "declined", "0"}


def load_panel_prior(con, task_class: Optional[str]) -> Optional[dict]:
    """Return the most-recent non-rejected panel's entry for `task_class`.

    Walks panel_decisions newest-first (week_start, then panel_id as tiebreak),
    skips any explicitly-rejected row, parses the recommendation JSON, and
    returns that class's dict ({"recommended", "recommended_q", "pareto", ...})
    or None if absent/unusable. Best-effort: any error -> None.
    """
    if not task_class:
        return None
    try:
        rows = con.execute(
            "SELECT recommendation, andy_decision FROM panel_decisions "
            "ORDER BY week_start DESC, panel_id DESC"
        ).fetchall()
    except sqlite3.Error as exc:  # table absent / schema drift
        logger.debug("panel_decisions unavailable: %s", exc)
        return None
    for row in rows:
        if _is_rejected(row["andy_decision"]):
            continue  # a vetoed panel must not block older advisory guidance
        raw = row["recommendation"]
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("malformed panel recommendation JSON: %s", exc)
            continue  # tolerate one bad row; try the next-newest panel
        if isinstance(rec, dict):
            entry = rec.get(task_class)
            if isinstance(entry, dict):
                return entry
        return None  # newest usable panel simply has no entry for this class
    return None


def _bakeoff_class_for(task_class: Optional[str]) -> Optional[str]:
    """Invert CHAMPION_CLASS_MAP: 24-class underscored -> bake-off class, or None."""
    if not task_class:
        return None
    for bake_class, under_class in CHAMPION_CLASS_MAP.items():
        if under_class == task_class:
            return bake_class
    return None


def load_champion_model(con, task_class: Optional[str]) -> Optional[str]:
    """Active champion model_id for a DEFENSIBLY-mapped class, else None.

    Returns None when the class has no entry in CHAMPION_CLASS_MAP (e.g.
    "reasoning") so we never apply a guessed mapping. Best-effort: errors -> None.
    """
    bake_class = _bakeoff_class_for(task_class)
    if not bake_class:
        return None  # unmapped / ambiguous class -> skip champions entirely
    try:
        row = con.execute(
            "SELECT model_id FROM champions "
            "WHERE task_class=? AND dethroned_at IS NULL "
            "ORDER BY promoted_at DESC LIMIT 1",
            (bake_class,),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("champions unavailable: %s", exc)
        return None
    return row["model_id"] if row else None


def load_champion_route(con, task_class: Optional[str]) -> Optional[str]:
    """Resolve the active champion (mapped class) to a concrete route_id, or None.

    The champion record names a MODEL; this picks a representative enabled route
    serving that model. The boost itself matches on model_id (see
    champion_panel_boost) so every route of the champion model benefits, but this
    accessor satisfies the route_id-shaped contract. Best-effort: errors -> None.
    """
    mid = load_champion_model(con, task_class)
    if not mid:
        return None
    try:
        row = con.execute(
            "SELECT route_id FROM routes WHERE model_id=? AND enabled=1 "
            "ORDER BY route_id LIMIT 1",
            (mid,),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("routes lookup for champion failed: %s", exc)
        return None
    return row["route_id"] if row else None


def _clamp(value: float, base: float) -> float:
    """Boost-only and hard-capped: never below `base`, never above HARD_CAP."""
    return min(HARD_CAP, max(base, value))


def champion_panel_boost(
    con, route, task_class: Optional[str], base_prior: float
) -> Tuple[float, Optional[str]]:
    """Apply a bounded, boost-only nudge to a warm-start prior.

    Precedence (strongest first), at most one fires:
      1. route is the panel's `recommended` route for the class
         -> adopt recommended_q if higher (max blend).
      2. route is on the panel `pareto` frontier with a q
         -> partial nudge toward that q (PARETO_BLEND).
      3. route's model is the active champion of a mapped class
         -> small fixed +CHAMPION_BOOST.
      4. otherwise -> unchanged.

    Returns (adjusted_prior, reason | None). The adjusted prior is always within
    [base_prior, HARD_CAP]. `reason` is a short human-readable string for the
    shadow rationale (transparency is the point of U4) and is None on no-op.
    Best-effort: any internal error falls back to (base_prior, None).
    """
    try:
        route_id = route["route_id"]
        model_id = route["model_id"]

        panel = load_panel_prior(con, task_class)
        if panel:
            # 1) explicit panel recommendation -> strongest nudge
            if panel.get("recommended") == route_id:
                rec_q = panel.get("recommended_q")
                if rec_q is not None:
                    adjusted = _clamp(max(base_prior, float(rec_q)), base_prior)
                    return adjusted, f"panel-recommended q={rec_q}"

            # 2) on the Pareto frontier -> smaller partial nudge
            for entry in panel.get("pareto") or []:
                if entry.get("route") == route_id and entry.get("q") is not None:
                    target = min(HARD_CAP, float(entry["q"]))
                    nudged = base_prior + (target - base_prior) * PARETO_BLEND
                    adjusted = _clamp(nudged, base_prior)
                    return adjusted, f"panel-pareto q={entry['q']}"

        # 3) active champion of a DEFENSIBLY-mapped class -> small fixed boost
        champ_model = load_champion_model(con, task_class)
        if champ_model and model_id == champ_model:
            adjusted = _clamp(base_prior + CHAMPION_BOOST, base_prior)
            return adjusted, f"champion({task_class})"

    except Exception as exc:  # never raise into route selection
        logger.debug("champion_panel_boost fell back to base: %s", exc)

    return base_prior, None
