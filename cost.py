"""Argos effective-cost model.

effective_cost(route_id, task) = cash_cost + capacity_cost + congestion_cost

This is the cost half of route selection. It implements the shadow-pricing the
research prescribed: sunk/capped lanes are NEVER priced at literal zero, so the
optimiser does not burn scarce quota on trivial work and then pay peak prices
for important work.

Capacity cost uses a piecewise-CONVEX soft-reservation price (not linear -
linear is too easy to game near the reserve), scaled by a per-window primal-dual
pacing multiplier lambda_w and a use-it-or-lose-it time term tau^gamma.

All tunables are module-level constants. See
_Homelab/canonical/argos-router-research-perplexity-2026-05-30.md for the
derivation and citations.
"""
from __future__ import annotations
import sqlite3, datetime
from dataclasses import dataclass

DB_PATH = "/home/andy/argos/argos.db"

# --- tunable constants (research defaults) ---
BUFFER_FRAC = 0.15        # b = 0.15 * limit (buffer band above reserve)
KAPPA_MULT = 5.0          # kappa = 5 * eta (steepness once into reserve)
GAMMA = 1.0               # psi(tau) = tau^gamma (use-it-or-lose-it decay)
NOMINAL_CAPACITY_COST = 0.0005   # charged to capped/sunk lanes when limit unknown,
                                 # so they are cheap-but-never-zero (in USD-ish units)
DEFAULT_EST_INPUT = 4000  # fallback token estimates if task omits them
DEFAULT_EST_OUTPUT = 1500


@dataclass
class Task:
    est_input_tokens: int = DEFAULT_EST_INPUT
    est_output_tokens: int = DEFAULT_EST_OUTPUT
    task_class: str | None = None     # for stakes-based reserve (future)


def _conn():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def cash_cost(con, route, task: Task) -> float:
    """Per-token cash cost via routes.model_id -> model_prices. 0 for sunk/flat-rate."""
    cost_mode = route["cost_mode"]
    if cost_mode in ("sunk", "flat_rate_capped"):
        return 0.0  # no cash; capacity_cost carries their shadow price
    mid = route["model_id"]
    if not mid:
        return 0.0
    row = con.execute(
        "SELECT input_per_1m_usd, output_per_1m_usd, request_overhead_usd "
        "FROM model_prices WHERE model_id=?", (mid,)).fetchone()
    if not row:
        return 0.0
    in_p, out_p, overhead = (row[0] or 0), (row[1] or 0), (row[2] or 0)
    return (task.est_input_tokens/1e6)*in_p + (task.est_output_tokens/1e6)*out_p + overhead


def _phi(R, reserve, limit):
    """Piecewise-convex soft-reservation shape. R=remaining, reserve=R*, limit=L."""
    b = BUFFER_FRAC * limit
    # eta = penalty at the reserve edge; set per-call to median cheap fallback (passed in)
    # here phi returns the SHAPE in units of eta; caller multiplies. We return a
    # normalized value where eta=1 at the edge.
    if R >= reserve + b:
        return 0.0
    if R > reserve:  # in the buffer band: quadratic ramp 0 -> 1
        return ((reserve + b - R) / b) ** 2
    # below reserve: 1 + kappa-scaled quadratic (steep)
    return 1.0 + KAPPA_MULT * ((reserve - R) / max(1.0, reserve)) ** 2


def capacity_cost(con, route, task: Task, eta: float | None = None) -> float:
    """Shadow price for capped/sunk lanes. Convex soft-reservation * pacing * time."""
    cost_mode = route["cost_mode"]
    if cost_mode == "per_token":
        return 0.0  # per-token lanes have no capacity constraint
    rid = route["route_id"]
    caps = con.execute(
        "SELECT window, limit_units, used_units, reserve_target, lambda_w, "
        "window_length_sec, resets_at FROM route_capacity WHERE route_id=?",
        (rid,)).fetchall()
    if not caps:
        return NOMINAL_CAPACITY_COST
    # eta default: median cash cost of a decent per-token fallback (computed once)
    if eta is None:
        eta = _median_fallback_cash(con, task)
    # take the MAX capacity cost across windows (the tightest binding window dominates)
    worst = 0.0
    for (win, limit, used, reserve, lam, win_len, resets_at) in caps:
        if limit is None:
            # limit unknown: charge nominal so the lane is cheap-but-nonzero
            worst = max(worst, NOMINAL_CAPACITY_COST)
            continue
        R = limit - (used or 0)
        shape = _phi(R, reserve or 0, limit)        # in units of eta
        # tau = fraction of window remaining; if unknown, assume mid-window (0.5)
        tau = 0.5
        if resets_at:
            try:
                rt = datetime.datetime.fromisoformat(resets_at)
                now = datetime.datetime.now(rt.tzinfo)
                remaining = max(0.0, (rt - now).total_seconds())
                tau = min(1.0, remaining / win_len) if win_len else 0.5
            except Exception:
                tau = 0.5
        psi = tau ** GAMMA
        lam_eff = max(0.0, lam or 0.0)
        # base price = eta*shape; pacing multiplier adds on top; time scales it.
        price = (eta * shape) * (1.0 + lam_eff) * psi
        # floor at nominal so even a fresh capped lane is never literally free
        price = max(price, NOMINAL_CAPACITY_COST)
        worst = max(worst, price)
    return worst


def _median_fallback_cash(con, task: Task) -> float:
    """Median cash cost of enabled per-token routes - the 'eta' anchor."""
    rows = con.execute(
        "SELECT model_id FROM routes WHERE cost_mode='per_token' AND enabled=1").fetchall()
    costs = []
    for (mid,) in rows:
        if not mid:
            continue
        p = con.execute("SELECT input_per_1m_usd, output_per_1m_usd, request_overhead_usd "
                        "FROM model_prices WHERE model_id=?", (mid,)).fetchone()
        if p:
            c = (task.est_input_tokens/1e6)*(p[0] or 0) + (task.est_output_tokens/1e6)*(p[1] or 0) + (p[2] or 0)
            if c > 0:
                costs.append(c)
    if not costs:
        return 0.01  # safe default eta
    costs.sort()
    return costs[len(costs)//2]


def congestion_cost(con, route, task: Task) -> float:
    """Latency/queue penalty when a lane is busy. STUB - returns 0 for now.
    TODO: derive from recent latency_ms in dispatches per route + in-flight count."""
    return 0.0


def effective_cost(route_id: str, task: Task | None = None, con=None) -> float:
    own = False
    if con is None:
        con = _conn(); con.row_factory = sqlite3.Row; own = True
    if task is None:
        task = Task()
    try:
        route = con.execute(
            "SELECT route_id, backend, tool, access_path, model_id, cost_mode, enabled "
            "FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise ValueError(f"unknown route_id: {route_id}")
        eta = _median_fallback_cash(con, task)
        return (cash_cost(con, route, task)
                + capacity_cost(con, route, task, eta=eta)
                + congestion_cost(con, route, task))
    finally:
        if own:
            con.close()


def rank_routes(task: Task | None = None):
    """Print effective_cost for every enabled route, cheapest first - for inspection."""
    con = _conn(); con.row_factory = sqlite3.Row
    if task is None:
        task = Task()
    rows = con.execute("SELECT route_id, cost_mode FROM routes WHERE enabled=1").fetchall()
    scored = []
    for r in rows:
        scored.append((effective_cost(r["route_id"], task, con), r["route_id"], r["cost_mode"]))
    scored.sort()
    print(f"=== effective_cost ranking (est {task.est_input_tokens}in/{task.est_output_tokens}out tokens) ===")
    for cost, rid, mode in scored:
        print(f"  ${cost:.6f}  {rid:42s} [{mode}]")
    con.close()


if __name__ == "__main__":
    rank_routes()
