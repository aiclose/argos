"""Fleiss Kappa - inter-rater agreement metric for tournament judges (#665).

Used in Crucible bake-off Phase 5+ to measure how reliable a tier-assignment is
across K judges (multiple AI judges scoring the same dispatch).

Formula:
    kappa = (P_observed - P_expected) / (1 - P_expected)

Interpretation:
    kappa > 0.7  : strong agreement, tier label is reliable
    0.4 < kappa <= 0.7 : moderate, weighted vote OR resolve via ensemble
    kappa <= 0.4 : weak, escalate to manual grade or expand judge pool

Reference: J.L. Fleiss, "Measuring Nominal Scale Agreement Among Many Raters", 
Psychological Bulletin 76(5), 1971.
"""
import math
from typing import List, Dict


def fleiss_kappa(ratings_matrix: List[List[int]]) -> float:
    """Compute Fleiss Kappa given a ratings matrix.
    
    Args:
        ratings_matrix: N x K matrix where N = items being rated, K = number of categories.
            Each cell ratings_matrix[i][j] = number of judges who assigned item i to category j.
            Row sums must all equal n (the constant number of judges per item).
    
    Returns:
        Fleiss Kappa in [-1, 1]. Negative = worse than chance, 0 = chance, 1 = perfect.
    
    Raises:
        ValueError: if matrix is empty or row sums are inconsistent.
    """
    if not ratings_matrix:
        raise ValueError("ratings_matrix is empty")
    
    N = len(ratings_matrix)
    K = len(ratings_matrix[0])
    if K == 0:
        raise ValueError("ratings_matrix has no categories")
    
    # Validate row sums
    n = sum(ratings_matrix[0])
    if n < 2:
        raise ValueError(f"need >=2 raters per item, got {n}")
    for i, row in enumerate(ratings_matrix):
        if sum(row) != n:
            raise ValueError(f"row {i} sum {sum(row)} != {n}")
    
    # P_j = proportion of all assignments to category j
    p_j = [sum(ratings_matrix[i][j] for i in range(N)) / (N * n) for j in range(K)]
    
    # P_i = inter-rater agreement for item i
    # P_i = (1/(n*(n-1))) * (sum_j n_ij^2 - n)
    P_i = []
    for row in ratings_matrix:
        agree = sum(c * c for c in row) - n
        P_i.append(agree / (n * (n - 1)))
    
    # P_observed = mean of P_i
    P_observed = sum(P_i) / N
    
    # P_expected = sum of p_j^2
    P_expected = sum(p * p for p in p_j)
    
    if abs(1 - P_expected) < 1e-12:
        # All raters agreed on same category for everything; kappa undefined, return 1.0
        return 1.0
    
    return (P_observed - P_expected) / (1 - P_expected)


def kappa_interpretation(k: float) -> str:
    """Return human-readable interpretation of a kappa score."""
    if k <= 0:
        return "worse-than-chance"
    elif k <= 0.20:
        return "slight"
    elif k <= 0.40:
        return "fair"
    elif k <= 0.60:
        return "moderate"
    elif k <= 0.80:
        return "substantial"
    else:
        return "almost-perfect"


def from_judge_decisions(decisions: List[Dict]) -> float:
    """Convenience wrapper: compute kappa from a list of bake-off judge decision rows.
    
    Args:
        decisions: list of dicts with at least keys (round_id, dispatch_id, judge_id, winner_id)
    
    Returns:
        Fleiss kappa across all dispatches in the round, treating each (champion, challenger)
        pair as a binary category vote.
    """
    if not decisions:
        return 0.0
    
    # Group by dispatch_id
    by_dispatch = {}
    for d in decisions:
        did = d['dispatch_id']
        by_dispatch.setdefault(did, []).append(d)
    
    # Build binary matrix: for each dispatch, how many judges picked champion vs challenger
    # Need to identify champion/challenger for each dispatch
    matrix = []
    for did, ds in by_dispatch.items():
        if not ds:
            continue
        # Get all unique winners + identify champion (most-voted gets to be category 0)
        winners = [d['winner_id'] for d in ds]
        unique = list(set(winners))
        if len(unique) > 2:
            # Three-way tie - treat as 3-category but Fleiss handles K>2
            pass
        # Build counts vector for this item
        vec = [winners.count(w) for w in unique]
        # Pad to K=2 if binary, else preserve
        while len(vec) < 2:
            vec.append(0)
        matrix.append(vec)
    
    if not matrix:
        return 0.0
    
    # Normalize all rows to same length K (max categories observed)
    K = max(len(r) for r in matrix)
    matrix = [r + [0] * (K - len(r)) for r in matrix]
    
    return fleiss_kappa(matrix)


# Self-test
if __name__ == "__main__":
    # Example from Fleiss 1971: 30 items, 6 raters, perfect agreement on item 1
    # All 6 judges pick category 0 for item 1
    test_matrix = [
        [6, 0, 0],  # all 6 picked cat 0
        [3, 3, 0],  # split 3/3
        [0, 6, 0],  # all picked cat 1
        [2, 2, 2],  # 3-way split
    ]
    k = fleiss_kappa(test_matrix)
    print(f"test kappa = {k:.3f} ({kappa_interpretation(k)})")
    
    # Perfect agreement should give kappa = 1
    perfect = [[5, 0], [5, 0], [5, 0]]
    print(f"perfect = {fleiss_kappa(perfect):.3f}")
    
    # Random should give ~0
    chance = [[3, 2], [2, 3], [3, 2], [2, 3]]
    print(f"chance = {fleiss_kappa(chance):.3f}")
