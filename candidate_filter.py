"""Candidate Filter - pre-LP-solver eligibility check for Argos Phase 4 (#660).

Filters models out of LP solver consideration when they fail capability gates,
saving solver time and preventing nonsense routing decisions.

Gates:
1. context_window < required_input_tokens (cannot fit prompt)
2. supports_tools = False AND task requires tools
3. supports_vision = False AND task requires images
4. deprecated = True (model retired or quality-demoted per #669)
5. tier > max_tier (cost ceiling)
6. < 10 historical dispatches in last 90 days (cold-start protection)
"""
import sqlite3
from typing import Dict, List, Optional


COLD_START_DISPATCH_THRESHOLD = 10
COLD_START_RECENCY_DAYS = 90


def filter_candidates(
    db: sqlite3.Connection,
    task_class: str,
    required_input_tokens: int,
    requires_tools: bool = False,
    requires_vision: bool = False,
    max_tier: Optional[str] = None,
) -> List[Dict]:
    query = """
        SELECT mp.model_id, mp.provider, mp.input_per_1m_usd, mp.output_per_1m_usd,
               mp.tier, mp.supports_tools, mp.supports_vision, mp.context_window,
               COALESCE(mp.deprecated, 0) AS deprecated,
               (SELECT COUNT(*) FROM dispatches d WHERE d.model_used = mp.model_id
                  AND d.ts >= datetime('now', '-90 days')) AS recent_dispatch_count
        FROM model_prices mp
        WHERE COALESCE(mp.deprecated, 0) = 0
    """
    rows = db.execute(query).fetchall()
    
    tier_order = {"tier_0": 0, "tier_1": 1, "tier_2": 2, "tier_3": 3, "tier_4": 4}
    max_tier_rank = tier_order.get(max_tier, 99) if max_tier else 99
    
    eligible: List[Dict] = []
    
    for row in rows:
        (model_id, provider, in_cost, out_cost, tier,
         tools_ok, vision_ok, ctx_window, deprecated, recent_count) = row
        
        if ctx_window and ctx_window < required_input_tokens:
            continue
        if requires_tools and not tools_ok:
            continue
        if requires_vision and not vision_ok:
            continue
        if deprecated:
            continue
        if tier_order.get(tier, 99) > max_tier_rank:
            continue
        if recent_count < COLD_START_DISPATCH_THRESHOLD:
            continue
        
        eligible.append({
            "model_id": model_id,
            "provider": provider,
            "input_per_1m_usd": in_cost,
            "output_per_1m_usd": out_cost,
            "tier": tier,
            "supports_tools": bool(tools_ok),
            "supports_vision": bool(vision_ok),
            "context_window": ctx_window,
            "dispatch_count": recent_count,
        })
    
    return eligible


def filter_with_explanation(db, task_class, required_input_tokens,
                             requires_tools=False, requires_vision=False,
                             max_tier=None):
    eligible = filter_candidates(db, task_class, required_input_tokens,
                                  requires_tools, requires_vision, max_tier)
    eligible_ids = set(m["model_id"] for m in eligible)
    
    query = """
        SELECT mp.model_id, mp.context_window, mp.supports_tools, mp.supports_vision,
               mp.tier, COALESCE(mp.deprecated, 0) AS deprecated,
               (SELECT COUNT(*) FROM dispatches d WHERE d.model_used = mp.model_id
                  AND d.ts >= datetime('now', '-90 days')) AS recent_dispatch_count
        FROM model_prices mp
    """
    all_rows = db.execute(query).fetchall()
    
    tier_order = {"tier_0": 0, "tier_1": 1, "tier_2": 2, "tier_3": 3, "tier_4": 4}
    max_tier_rank = tier_order.get(max_tier, 99) if max_tier else 99
    
    rejected = []
    for (mid, ctx, tools, vision, tier, dep, recent) in all_rows:
        if mid in eligible_ids:
            continue
        reasons = []
        if dep:
            reasons.append("deprecated")
        if ctx and ctx < required_input_tokens:
            reasons.append("ctx_too_small")
        if requires_tools and not tools:
            reasons.append("no_tools")
        if requires_vision and not vision:
            reasons.append("no_vision")
        if tier_order.get(tier, 99) > max_tier_rank:
            reasons.append("tier_too_high")
        if recent < COLD_START_DISPATCH_THRESHOLD:
            reasons.append("cold_start")
        if reasons:
            rejected.append((mid, ", ".join(reasons)))
    
    return {"eligible": list(eligible_ids), "rejected": rejected}


if __name__ == "__main__":
    db = sqlite3.connect("/home/andy/argos/argos.db")
    print("=== Candidate Filter Self-Test ===")
    
    r = filter_with_explanation(db, "general", 4000)
    e_count = len(r["eligible"])
    rj_count = len(r["rejected"])
    print(f"general 4k tokens: eligible={e_count} rejected={rj_count}")
    if r["eligible"]:
        print(f"  sample: {r['eligible'][:5]}")
    
    r = filter_with_explanation(db, "code", 100000)
    print(f"code 100k tokens: eligible={len(r['eligible'])} rejected={len(r['rejected'])}")
    
    r = filter_with_explanation(db, "general", 4000, max_tier="tier_2")
    print(f"general tier<=2: eligible={len(r['eligible'])} rejected={len(r['rejected'])}")
    
    r = filter_with_explanation(db, "general", 4000, requires_tools=True)
    print(f"general+tools: eligible={len(r['eligible'])} rejected={len(r['rejected'])}")
    
    db.close()
