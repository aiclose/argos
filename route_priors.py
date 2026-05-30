"""Argos route success priors, warm-started from published benchmarks.

The router needs per-route success estimates, but we have almost no route-spread
outcome data yet. Rather than wait (or assign noise), we WARM-START each route
from its model's known coding-benchmark performance, exactly the cold-start
strategy the research prescribed (benchmark priors + model-family / size class).
Real logged outcomes override these seeds as they accumulate.

Sources (all ~May 2026, coding benchmarks): SWE-bench Verified, Aider Polyglot,
SWE-rebench, and the AkitaOnRails Chinese-model coding benchmark. Scores
normalised to an approximate coding success probability on [0,1]. These are
STALE SEEDS by design, a starting prior, not ground truth. See
argos-router-research-perplexity-2026-05-30.md (warm-start section).

Numbers are deliberately coarse (rounded) to signal they are priors, not
measurements.
"""

# Base coding-success prior per model_id (0-1). Keyed on the model_id used in
# the routes table. Frontier ~0.85-0.90, strong-open ~0.75-0.87, mid ~0.60-0.70,
# weak/small ~0.35-0.55.
BENCH_PRIOR = {
    # --- frontier (Zen / Pharos) ---
    "openai/gpt-5.5": 0.89,
    "openai/gpt-5.4": 0.86,
    "openai/gpt-5.4-mini": 0.74,
    "openai/gpt-5.3-codex": 0.85,
    "openai/gpt-5.2": 0.83,
    "openai/gpt-5.1": 0.82,
    "openai/gpt-5": 0.80,
    "anthropic/claude-opus-4.5": 0.89,
    "anthropic/claude-sonnet-4.5": 0.82,
    "anthropic/claude-sonnet-4": 0.78,
    "google/gemini-3.1-pro-preview": 0.80,
    "google/gemini-3.5-flash": 0.72,
    "google/gemini-2.5-pro": 0.74,
    "google/gemini-2.5-flash": 0.66,
    # --- strong open (OpenCode Go) ---
    "moonshotai/kimi-k2.6": 0.84,
    "moonshotai/kimi-k2.6:free": 0.84,
    "moonshotai/kimi-k2.5": 0.69,
    "deepseek/deepseek-v4-pro": 0.80,
    "deepseek/deepseek-v4-flash": 0.78,
    "deepseek/deepseek-v4-flash:free": 0.78,
    "deepseek/deepseek-chat": 0.74,      # deepseek direct (V3-class)
    "qwen/qwen3.7-max": 0.76,
    "qwen/qwen3.6-plus": 0.71,
    "xiaomi/mimo-v2.5-pro": 0.67,
    "xiaomi/mimo-v2.5": 0.60,
    # --- mid / lower open (Go) ---
    "z-ai/glm-5": 0.64,
    "z-ai/glm-5.1": 0.50,                # smaller/cheaper variant; benched ~0.46-0.50
    "z-ai/glm-4.5-air:free": 0.45,
    "minimax/minimax-m2.7": 0.45,
    "minimax/minimax-m2.5": 0.50,
    "minimax/minimax-m2.5:free": 0.50,
    "openai/gpt-oss-120b:free": 0.58,
    "openai/gpt-oss-20b:free": 0.42,
    "qwen/qwen3-coder:free": 0.48,
    "qwen/qwen3-next-80b-a3b-instruct:free": 0.45,
    "google/gemma-4-31b-it:free": 0.40,
    "google/gemma-4-26b-a4b-it:free": 0.38,
}

# Family fallback when a specific model_id is not in the table.
FAMILY_PRIOR = {
    "anthropic": 0.82, "openai": 0.80, "google": 0.70,
    "moonshotai": 0.72, "deepseek": 0.74, "qwen": 0.62,
    "xiaomi": 0.60, "z-ai": 0.55, "minimax": 0.48,
}

# Sunk-cost CLI tools without a model_id: prior by tool reputation.
TOOL_PRIOR = {
    "codex-cli": 0.85,      # GPT-5.x-codex class
    "claude-code": 0.86,    # Claude Opus/Sonnet class
}

# Task-class difficulty modulation. Benchmarks measure hard coding; for easy
# classes the spread compresses upward (even weak models cope), for hard classes
# it stretches down. We blend the model prior toward the class floor by a weight
# that depends on stakes: low-stakes -> pull toward an easy-task ceiling;
# high-stakes -> trust the raw coding prior.
EASY_CEILING = 0.92   # even modest models usually clear easy tasks
def class_adjust(base_prior: float, error_sensitivity: str) -> float:
    es = (error_sensitivity or "medium").lower()
    if es in ("low",):
        # compress upward: weak models look better on easy work
        return base_prior + (EASY_CEILING - base_prior) * 0.5
    if es in ("high", "critical"):
        # trust the hard-coding prior as-is (maybe slight penalty for criticality)
        return base_prior * (0.97 if es == "critical" else 1.0)
    # medium: mild upward compression
    return base_prior + (EASY_CEILING - base_prior) * 0.2


def seed_prior(model_id: str, tool: str, error_sensitivity: str = "medium") -> float:
    """Return a warm-start success prior in [0,1] for a route."""
    base = None
    if model_id and model_id in BENCH_PRIOR:
        base = BENCH_PRIOR[model_id]
    elif model_id and "/" in model_id:
        fam = model_id.split("/")[0]
        base = FAMILY_PRIOR.get(fam)
    if base is None and tool in TOOL_PRIOR:
        base = TOOL_PRIOR[tool]
    if base is None:
        base = 0.60  # unknown route: neutral-ish
    return round(min(0.97, max(0.30, class_adjust(base, error_sensitivity))), 3)


if __name__ == "__main__":
    # quick sanity print
    for mid, tool in [("openai/gpt-5.5","opencode"), ("z-ai/glm-5.1","opencode"),
                      ("moonshotai/kimi-k2.6","opencode"), (None,"codex-cli"),
                      ("google/gemma-4-31b-it:free","opencode")]:
        for es in ("low","high"):
            print(f"{str(mid):40s} {tool:12s} {es:5s} -> {seed_prior(mid, tool, es)}")
