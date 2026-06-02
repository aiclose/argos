"""Stage 5 route-v2 verification fixtures.

Builds a temporary SQLite database and calls route_select.select_route directly.
The production DB is intentionally not required for this verification run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import route_select


def _init_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE task_classes (
            class_id TEXT PRIMARY KEY,
            default_quality_floor REAL
        );
        CREATE TABLE routes (
            route_id TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            tool TEXT NOT NULL,
            access_path TEXT NOT NULL,
            model_id TEXT,
            cost_mode TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            healthcheck_type TEXT,
            last_health TEXT,
            quota_status TEXT,
            capabilities TEXT,
            latency_ms REAL
        );
        CREATE TABLE model_prices (
            model_id TEXT PRIMARY KEY,
            input_per_1m_usd REAL,
            output_per_1m_usd REAL,
            request_overhead_usd REAL
        );
        CREATE TABLE route_capacity (
            route_id TEXT,
            window TEXT,
            limit_units REAL,
            used_units REAL,
            reserve_target REAL,
            lambda_w REAL,
            window_length_sec REAL,
            resets_at TEXT
        );
        CREATE TABLE dispatches (
            dispatch_id TEXT,
            model_used TEXT,
            task_class TEXT,
            accepted INTEGER
        );
        """
    )
    con.executemany(
        "INSERT INTO task_classes VALUES (?, ?)",
        [
            ("documentation", 0.70),
            ("code_generation", 0.70),
            ("debugging", 0.70),
        ],
    )
    con.executemany(
        "INSERT INTO model_prices VALUES (?, ?, ?, ?)",
        [
            ("openai/gpt-5.3-codex", 0.0, 0.0, 0.0),
            ("moonshotai/kimi-k2.6", 0.15, 0.60, 0.0),
            ("openai/gpt-5.3", 1.25, 10.0, 0.0),
        ],
    )
    con.executemany(
        """
        INSERT INTO routes
        (route_id, backend, tool, access_path, model_id, cost_mode, enabled,
         healthcheck_type, last_health, quota_status, capabilities, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "forge:codex-cli",
                "forge",
                "codex",
                "codex-cli",
                "openai/gpt-5.3-codex",
                "sunk",
                1,
                "cli-smoke",
                "ok",
                "ok",
                '["code"]',
                900.0,
            ),
            (
                "forge:stale-opencode-go:kimi-k2.6",
                "forge",
                "opencode",
                "opencode-go",
                "moonshotai/kimi-k2.6",
                "flat_rate_capped",
                1,
                "api-chat",
                "ok",
                "ok",
                '["code"]',
                100.0,
            ),
            (
                "janus:stale-openrouter:gpt-5.3",
                "janus",
                "openrouter",
                "api-chat",
                "openai/gpt-5.3",
                "per_token",
                1,
                "api-chat",
                "ok",
                "ok",
                '["chat"]',
                50.0,
            ),
        ],
    )
    con.commit()
    con.close()


def _plan_dict(tag: str, task: route_select.RouteTask) -> dict:
    plan = route_select.select_route(task)
    return {
        "tag": tag,
        "selected_route": plan.selected_route,
        "selected_model": plan.selected_model,
        "backend": plan.backend,
        "backend_reason": plan.backend_reason,
        "gated_backend": plan.gated_backend,
        "gate_reason": plan.gate_reason,
        "cost_mode": plan.cost_mode,
        "effective_cost_usd": plan.effective_cost,
        "quality_floor": plan.quality_floor,
        "predicted_success": plan.predicted_success,
        "cleared_floor": plan.cleared_floor,
        "scorer_score": plan.scorer_score,
        "scored_choice": {
            "selected_route": plan.selected_route,
            "selected_model": plan.selected_model,
            "cost_mode": plan.cost_mode,
            "effective_cost_usd": plan.effective_cost,
            "predicted_success": plan.predicted_success,
            "cleared_floor": plan.cleared_floor,
            "scorer_score": plan.scorer_score,
        }
        if plan.error is None
        else None,
        "error": plan.error,
        "fallback_chain": plan.fallback_chain,
        "candidate_count": plan.candidate_count,
        "rationale": plan.rationale,
    }


def _unverified_routes(path: str) -> list[dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT route_id, backend, healthcheck_type, last_health
        FROM routes
        WHERE enabled=1
          AND NOT (healthcheck_type='cli-smoke' AND last_health='ok')
        ORDER BY route_id
        """
    ).fetchall()
    con.close()
    return [dict(row) for row in rows]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "argos-stage5-fixtures.db")
        _init_db(db_path)
        route_select.DB_PATH = db_path

        cases = [
            (
                "a_explicit_forge_override",
                route_select.RouteTask(
                    tag="a_explicit_forge_override",
                    task_class="documentation",
                    explicit_backend_override="Forge",
                    predicates={
                        "text_only_output": True,
                        "single_turn_sufficient": True,
                    },
                    prompt="explain the local design",
                ),
            ),
            (
                "b_unambiguous_exec_verb_no_predicates",
                route_select.RouteTask(
                    tag="b_unambiguous_exec_verb_no_predicates",
                    prompt="apply the patch and run the tests",
                ),
            ),
            (
                "c_ambiguous_chat",
                route_select.RouteTask(
                    tag="c_ambiguous_chat",
                    prompt="Can you help me think through this?",
                ),
            ),
            (
                "d_stale_health_would_have_won",
                route_select.RouteTask(
                    tag="d_stale_health_would_have_won",
                    task_class="code_generation",
                    predicates={"needs_command_exec": True},
                    prompt="run the implementation",
                ),
            ),
        ]

        out = {
            "fixtures": [_plan_dict(tag, task) for tag, task in cases],
            "unverified_exclusions": _unverified_routes(db_path),
        }
        print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
