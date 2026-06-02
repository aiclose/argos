# Stage 5 Verify

Command run from `/work/argos`:

```sh
python3 stage5_route_fixtures.py
```

The production DB path `/home/andy/argos/argos.db` was not present in this workspace, so the verification used a temporary SQLite fixture and called `route_select.select_route` directly.

## Four Fixture Results Verbatim

```json
{
  "fixtures": [
    {
      "backend": "forge",
      "backend_reason": "override",
      "candidate_count": 1,
      "cleared_floor": true,
      "cost_mode": "sunk",
      "effective_cost_usd": 0.0005,
      "error": null,
      "fallback_chain": [],
      "gate_reason": "override",
      "gated_backend": "forge",
      "predicted_success": 0.864,
      "quality_floor": 0.7,
      "rationale": "backend=forge reason=override | task_class=documentation | floor=0.70 | strategy=within-backend-weighted-argmax | weights={'health': 0.35, 'quota': 0.2, 'quality': 0.2, 'cost': 0.15, 'latency': 0.05, 'path_preference': 0.05} | health_key=healthcheck_type:cli-smoke,last_health:ok | candidates=1 excluded={'backend': 1, 'health': 1, 'quota': 0, 'capability': 0} | picked forge:codex-cli score=0.963 eff=$0.000500 psucc=0.86",
      "scored_choice": {
        "cleared_floor": true,
        "cost_mode": "sunk",
        "effective_cost_usd": 0.0005,
        "predicted_success": 0.864,
        "scorer_score": 0.9628,
        "selected_model": "openai/gpt-5.3-codex",
        "selected_route": "forge:codex-cli"
      },
      "scorer_score": 0.9628,
      "selected_model": "openai/gpt-5.3-codex",
      "selected_route": "forge:codex-cli",
      "tag": "a_explicit_forge_override"
    },
    {
      "backend": "forge",
      "backend_reason": "lexical-inferred",
      "candidate_count": 1,
      "cleared_floor": true,
      "cost_mode": "sunk",
      "effective_cost_usd": 0.0005,
      "error": null,
      "fallback_chain": [],
      "gate_reason": "lexical-inferred",
      "gated_backend": "forge",
      "predicted_success": 0.864,
      "quality_floor": 0.7,
      "rationale": "backend=forge reason=lexical-inferred | task_class=None | floor=0.70 | strategy=within-backend-weighted-argmax | weights={'health': 0.35, 'quota': 0.2, 'quality': 0.2, 'cost': 0.15, 'latency': 0.05, 'path_preference': 0.05} | health_key=healthcheck_type:cli-smoke,last_health:ok | candidates=1 excluded={'backend': 1, 'health': 1, 'quota': 0, 'capability': 0} | picked forge:codex-cli score=0.963 eff=$0.000500 psucc=0.86",
      "scored_choice": {
        "cleared_floor": true,
        "cost_mode": "sunk",
        "effective_cost_usd": 0.0005,
        "predicted_success": 0.864,
        "scorer_score": 0.9628,
        "selected_model": "openai/gpt-5.3-codex",
        "selected_route": "forge:codex-cli"
      },
      "scorer_score": 0.9628,
      "selected_model": "openai/gpt-5.3-codex",
      "selected_route": "forge:codex-cli",
      "tag": "b_unambiguous_exec_verb_no_predicates"
    },
    {
      "backend": "janus",
      "backend_reason": "ambiguous",
      "candidate_count": 0,
      "cleared_floor": false,
      "cost_mode": null,
      "effective_cost_usd": 0.0,
      "error": {
        "code": "no_janus_route_available",
        "message": "no Janus route available"
      },
      "fallback_chain": [],
      "gate_reason": "ambiguous",
      "gated_backend": "janus",
      "predicted_success": 0.0,
      "quality_floor": 0.7,
      "rationale": "backend=janus reason=ambiguous | health_key=healthcheck_type:cli-smoke,last_health:ok | excluded={'backend': 2, 'health': 1, 'quota': 0, 'capability': 0} | no Janus route available",
      "scored_choice": null,
      "scorer_score": 0.0,
      "selected_model": null,
      "selected_route": null,
      "tag": "c_ambiguous_chat"
    },
    {
      "backend": "forge",
      "backend_reason": "forge_predicate",
      "candidate_count": 1,
      "cleared_floor": true,
      "cost_mode": "sunk",
      "effective_cost_usd": 0.0005,
      "error": null,
      "fallback_chain": [],
      "gate_reason": "forge_predicate",
      "gated_backend": "forge",
      "predicted_success": 0.864,
      "quality_floor": 0.7,
      "rationale": "backend=forge reason=forge_predicate | task_class=code_generation | floor=0.70 | strategy=within-backend-weighted-argmax | weights={'health': 0.35, 'quota': 0.2, 'quality': 0.2, 'cost': 0.15, 'latency': 0.05, 'path_preference': 0.05} | health_key=healthcheck_type:cli-smoke,last_health:ok | candidates=1 excluded={'backend': 1, 'health': 1, 'quota': 0, 'capability': 0} | picked forge:codex-cli score=0.963 eff=$0.000500 psucc=0.86",
      "scored_choice": {
        "cleared_floor": true,
        "cost_mode": "sunk",
        "effective_cost_usd": 0.0005,
        "predicted_success": 0.864,
        "scorer_score": 0.9628,
        "selected_model": "openai/gpt-5.3-codex",
        "selected_route": "forge:codex-cli"
      },
      "scorer_score": 0.9628,
      "selected_model": "openai/gpt-5.3-codex",
      "selected_route": "forge:codex-cli",
      "tag": "d_stale_health_would_have_won"
    }
  ],
  "unverified_exclusions": [
    {
      "backend": "forge",
      "healthcheck_type": "api-chat",
      "last_health": "ok",
      "route_id": "forge:stale-opencode-go:kimi-k2.6"
    },
    {
      "backend": "janus",
      "healthcheck_type": "api-chat",
      "last_health": "ok",
      "route_id": "janus:stale-openrouter:gpt-5.3"
    }
  ]
}
```

## Gate-Precedence Proof

Fixture `a_explicit_forge_override` sets `explicit_backend_override="Forge"` while also setting Janus-sufficient predicates (`text_only_output=true`, `single_turn_sufficient=true`) and `task_class="documentation"`, which maps to Janus. Actual output was `backend="forge"` and `backend_reason="override"`, proving explicit override wins before Janus predicates or task-class hints.

Fixture `b_unambiguous_exec_verb_no_predicates` supplies no predicates and starts the prompt with the hard execution verb `apply`. Actual output was `backend="forge"` and `backend_reason="lexical-inferred"`.

Fixture `c_ambiguous_chat` has no task class, no predicates, and no hard lexical verb. Actual output was `backend="janus"`, `backend_reason="ambiguous"`, and the hard-fail error `{"code": "no_janus_route_available", "message": "no Janus route available"}`. It did not fall back to Forge.

## Unverified Route Exclusions

Health is verified only by `healthcheck_type == "cli-smoke"` and `last_health == "ok"`.

Excluded routes:

| route_id | backend | excluded field |
| --- | --- | --- |
| `forge:stale-opencode-go:kimi-k2.6` | `forge` | `healthcheck_type="api-chat"` is not `cli-smoke` |
| `janus:stale-openrouter:gpt-5.3` | `janus` | `healthcheck_type="api-chat"` is not `cli-smoke` |

Fixture `d_stale_health_would_have_won` included the stale Forge route with `flat_rate_capped` cost mode and lower latency than `forge:codex-cli`, but it was excluded by the health field above. Actual output selected `forge:codex-cli`.

## Local Commit Hash

The local commit hash is reported after the commit is created. This file is part of that commit, so it cannot contain its own final hash without changing that hash.
