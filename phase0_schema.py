"""Argos Phase 0: data foundation on garage.
Creates argos.db schema, populates model_prices from OpenRouter, initializes empty tables.
Idempotent: safe to re-run."""

import sqlite3
import urllib.request
import json
import ssl
import os
import time
import sys

ARGOS_DB = "/home/andy/argos/argos.db"
SCHEMA_VERSION = 1

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def init_schema(db):
    """Create all Argos v2 tables."""
    schema = """
    -- Schema version for migrations
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Model registry: prices, capabilities, status
    CREATE TABLE IF NOT EXISTS model_prices (
        model_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        input_per_1m_usd REAL,
        output_per_1m_usd REAL,
        cached_input_per_1m_usd REAL,
        request_overhead_usd REAL DEFAULT 0,
        context_window INTEGER,
        max_output_tokens INTEGER,
        supports_tools BOOLEAN DEFAULT 0,
        supports_json_schema BOOLEAN DEFAULT 0,
        supports_vision BOOLEAN DEFAULT 0,
        tier TEXT,
        deprecated BOOLEAN DEFAULT 0,
        last_fetched_at TIMESTAMP,
        raw_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_model_prices_provider ON model_prices(provider);
    CREATE INDEX IF NOT EXISTS idx_model_prices_tier ON model_prices(tier);

    -- Task class taxonomy (hierarchical)
    CREATE TABLE IF NOT EXISTS task_classes (
        class_id TEXT PRIMARY KEY,
        parent_class_id TEXT,
        description TEXT,
        default_quality_floor REAL DEFAULT 0.70,
        default_error_sensitivity TEXT DEFAULT 'medium',
        FOREIGN KEY (parent_class_id) REFERENCES task_classes(class_id)
    );

    -- Dispatches: one row per LLM call (mirrors orchestrator cost-log + Argos extras)
    CREATE TABLE IF NOT EXISTS dispatches (
        dispatch_id TEXT PRIMARY KEY,
        ts TIMESTAMP NOT NULL,
        source TEXT,                       -- chat-claude, cron, n8n, etc
        provider_mode TEXT,                -- anthropic-direct, openrouter, or-native-agent, etc
        model_used TEXT,
        task_class TEXT,
        domain TEXT,
        complexity_score REAL,
        reasoning_depth REAL,
        ambiguity_score REAL,
        error_sensitivity TEXT,
        estimated_input_tokens INTEGER,
        estimated_output_tokens INTEGER,
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        actual_cost_usd REAL,
        latency_ms INTEGER,
        status TEXT,                       -- completed, errored, completed_no_checkpoint
        rework_cycles INTEGER DEFAULT 0,
        quality_score REAL,
        accepted BOOLEAN,
        FOREIGN KEY (task_class) REFERENCES task_classes(class_id),
        FOREIGN KEY (model_used) REFERENCES model_prices(model_id)
    );
    CREATE INDEX IF NOT EXISTS idx_dispatches_ts ON dispatches(ts);
    CREATE INDEX IF NOT EXISTS idx_dispatches_model ON dispatches(model_used);
    CREATE INDEX IF NOT EXISTS idx_dispatches_class ON dispatches(task_class);
    CREATE INDEX IF NOT EXISTS idx_dispatches_provider ON dispatches(provider_mode);

    -- Predictions: cost+quality estimates per dispatch (for shadow mode)
    CREATE TABLE IF NOT EXISTS predictions (
        prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispatch_id TEXT NOT NULL,
        predictor_version TEXT,
        predicted_cost_p50 REAL,
        predicted_cost_p90 REAL,
        predicted_cost_p95 REAL,
        predicted_quality REAL,
        predicted_success_prob REAL,
        candidate_models TEXT,             -- JSON array
        selected_model_id TEXT,
        fallback_chain TEXT,               -- JSON array of 3 ranked
        decision_rationale TEXT,
        was_exploration BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
    );
    CREATE INDEX IF NOT EXISTS idx_predictions_dispatch ON predictions(dispatch_id);

    -- Embeddings: 768D vectors for tasks (stored as BLOB float32)
    CREATE TABLE IF NOT EXISTS embeddings (
        dispatch_id TEXT PRIMARY KEY,
        embedding BLOB,
        embedding_model TEXT DEFAULT 'nomic-embed-text',
        dimension INTEGER DEFAULT 768,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
    );

    -- Bake-off rounds (weekly tournament)
    CREATE TABLE IF NOT EXISTS bake_off_rounds (
        round_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_date DATE NOT NULL,
        n_tasks INTEGER,
        n_judges INTEGER,
        fleiss_kappa REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Bake-off judges (8 in roster, 5 voting per round)
    CREATE TABLE IF NOT EXISTS bake_off_judges (
        judge_id TEXT PRIMARY KEY,
        display_name TEXT,
        provider_model TEXT,
        weight REAL DEFAULT 1.0,
        rolling_regret REAL,
        rolling_pareto_coverage REAL,
        active BOOLEAN DEFAULT 1,
        last_round_id INTEGER,
        FOREIGN KEY (last_round_id) REFERENCES bake_off_rounds(round_id)
    );

    -- Bake-off task allocations per judge
    CREATE TABLE IF NOT EXISTS bake_off_decisions (
        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        task_id TEXT NOT NULL,
        judge_id TEXT NOT NULL,
        recommended_tier TEXT,
        recommended_model TEXT,
        confidence REAL,
        predicted_failure_modes TEXT,      -- JSON array
        rationale TEXT,
        actual_cost_usd REAL,
        actual_quality REAL,
        regret REAL,
        FOREIGN KEY (round_id) REFERENCES bake_off_rounds(round_id),
        FOREIGN KEY (judge_id) REFERENCES bake_off_judges(judge_id)
    );

    -- Drift events
    CREATE TABLE IF NOT EXISTS drift_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_id TEXT NOT NULL,
        event_type TEXT,                    -- underperformance, recovery, deprecation, price_change
        consecutive_count INTEGER,
        magnitude_pct REAL,
        started_at TIMESTAMP,
        resolved_at TIMESTAMP,
        action_taken TEXT,
        FOREIGN KEY (model_id) REFERENCES model_prices(model_id)
    );

    -- 5-AI Strategy Panel weekly recommendations
    CREATE TABLE IF NOT EXISTS panel_decisions (
        panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start DATE NOT NULL,
        recommendation TEXT,                -- JSON: alpha delta, lambda delta, etc
        consensus_score REAL,
        andy_decision TEXT,                 -- 'approved' | 'vetoed' | 'modified'
        applied_at TIMESTAMP,
        notes TEXT
    );

    -- Manual grading queue (Andy's 30 min/wk)
    CREATE TABLE IF NOT EXISTS grading_queue (
        queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispatch_id TEXT NOT NULL,
        priority INTEGER DEFAULT 5,
        reason TEXT,                        -- low_confidence, judge_disagreement, drift_candidate
        graded_at TIMESTAMP,
        manual_quality_score REAL,
        manual_notes TEXT,
        FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
    );
    CREATE INDEX IF NOT EXISTS idx_grading_queue_priority ON grading_queue(priority DESC, queue_id);
    """
    db.executescript(schema)
    db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
               ("schema_version", str(SCHEMA_VERSION)))
    db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
               ("created_at", time.strftime("%Y-%m-%dT%H:%M:%S")))
    db.commit()
    n_tables = len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    log(f"Schema v{SCHEMA_VERSION} applied. Tables: {n_tables}")

def seed_task_classes(db):
    """Initial task class taxonomy (9 broad domains × subdomains)."""
    classes = [
        # Format: (class_id, parent_class_id, description, default_quality_floor, default_error_sensitivity)
        ("code_generation", None, "Writing new code from scratch", 0.85, "high"),
        ("code_implementation", "code_generation", "Implementing a clear feature", 0.85, "high"),
        ("code_boilerplate", "code_generation", "CRUD, scaffolding, well-known patterns", 0.70, "low"),
        ("code_algorithmic", "code_generation", "Novel algorithms, complex logic", 0.90, "high"),
        ("debugging", None, "Finding and fixing bugs", 0.85, "high"),
        ("debugging_simple", "debugging", "Bug with stack trace, clear cause", 0.75, "medium"),
        ("debugging_intermittent", "debugging", "Race conditions, flaky tests", 0.90, "high"),
        ("refactoring", None, "Restructuring without behavior change", 0.85, "high"),
        ("testing", None, "Test creation and validation", 0.70, "low"),
        ("test_unit", "testing", "Unit tests for existing code", 0.65, "low"),
        ("test_integration", "testing", "Integration tests, mocks", 0.75, "medium"),
        ("documentation", None, "Docs, READMEs, API references", 0.65, "low"),
        ("docs_api", "documentation", "API reference docs", 0.75, "medium"),
        ("docs_explainer", "documentation", "Conceptual explanations", 0.70, "low"),
        ("architecture_design", None, "System design, decisions", 0.95, "critical"),
        ("data_engineering", None, "Pipelines, ETL, schemas", 0.85, "high"),
        ("devops", None, "Infrastructure, deployment, CI/CD", 0.90, "high"),
        ("security", None, "Security review, audit, hardening", 0.95, "critical"),
        ("analysis", None, "Data analysis, reporting, summaries", 0.70, "low"),
        ("formatting", None, "Format conversion, schema mapping", 0.70, "low"),
        ("extraction", None, "Pulling structured data from text", 0.75, "medium"),
        ("creative", None, "Creative writing, brainstorming", 0.50, "low"),
        ("classification", None, "Categorization, labeling, tagging", 0.75, "medium"),
        ("conversation", None, "Q&A, chat-style help", 0.65, "low"),
    ]
    db.executemany("""
        INSERT OR REPLACE INTO task_classes
        (class_id, parent_class_id, description, default_quality_floor, default_error_sensitivity)
        VALUES (?, ?, ?, ?, ?)
    """, classes)
    db.commit()
    log(f"Seeded {len(classes)} task classes")

def populate_model_prices(db):
    """Fetch from OpenRouter and populate model_prices table."""
    log("Fetching OpenRouter models...")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "argos-phase0/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        data = json.load(r)

    models = data.get("data", [])
    log(f"Got {len(models)} models from OpenRouter")

    rows = []
    for m in models:
        mid = m.get("id", "")
        if not mid:
            continue
        provider = mid.split("/")[0] if "/" in mid else "unknown"
        pricing = m.get("pricing", {})
        try:
            in_p = float(pricing.get("prompt", "0")) * 1e6  # convert to $/1M
            out_p = float(pricing.get("completion", "0")) * 1e6
            cache_p = float(pricing.get("input_cache_read", "0")) * 1e6 if pricing.get("input_cache_read") else None
            req_p = float(pricing.get("request", "0")) if pricing.get("request") else 0
        except (ValueError, TypeError):
            in_p = out_p = cache_p = None
            req_p = 0
        ctx_window = m.get("context_length") or 0
        max_out = m.get("top_provider", {}).get("max_completion_tokens") or 0
        archt = m.get("architecture", {})
        modalities = archt.get("input_modalities") or []
        supports_vision = "image" in modalities

        # Tier inference (very rough - based on output price)
        if out_p is None:
            tier = "unknown"
        elif out_p >= 30:
            tier = "tier_1"
        elif out_p >= 3:
            tier = "tier_2"
        else:
            tier = "tier_3"

        rows.append((
            mid, provider, in_p, out_p, cache_p, req_p,
            ctx_window, max_out,
            0,  # supports_tools - need to derive
            0,  # supports_json_schema
            1 if supports_vision else 0,
            tier,
            0,  # deprecated
            time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(m)
        ))

    db.executemany("""
        INSERT OR REPLACE INTO model_prices
        (model_id, provider, input_per_1m_usd, output_per_1m_usd, cached_input_per_1m_usd, request_overhead_usd,
         context_window, max_output_tokens, supports_tools, supports_json_schema, supports_vision,
         tier, deprecated, last_fetched_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    db.commit()
    log(f"Populated {len(rows)} models into model_prices")

def seed_bake_off_judges(db):
    """Initial judge roster (8 Tier-1 models)."""
    judges = [
        ("opus_47", "Claude Opus 4.7", "anthropic/claude-opus-4-7"),
        ("gpt_5", "GPT-5", "openai/gpt-5"),
        ("gemini_3_pro", "Gemini 3 Pro", "google/gemini-3-pro"),
        ("kimi_k2", "Kimi K2", "moonshotai/kimi-k2"),
        ("deepseek_r1", "DeepSeek R1", "deepseek/deepseek-r1"),
        ("qwen_3_max", "Qwen 3 Max", "qwen/qwen3-max"),
        ("grok_4", "Grok 4", "xai/grok-4"),
        ("mistral_large_3", "Mistral Large 3", "mistralai/mistral-large-3"),
    ]
    for jid, name, model in judges:
        db.execute("""
            INSERT OR IGNORE INTO bake_off_judges (judge_id, display_name, provider_model, weight, active)
            VALUES (?, ?, ?, 1.0, 1)
        """, (jid, name, model))
    db.commit()
    log(f"Seeded {len(judges)} bake-off judges")

def main():
    os.makedirs(os.path.dirname(ARGOS_DB), exist_ok=True)
    log(f"Opening {ARGOS_DB}")
    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row

    init_schema(db)
    seed_task_classes(db)
    populate_model_prices(db)
    seed_bake_off_judges(db)

    # Summary
    print()
    log("=== ARGOS PHASE 0 SUMMARY ===")
    for table in ["schema_meta", "task_classes", "model_prices", "bake_off_judges",
                  "dispatches", "predictions", "embeddings", "bake_off_rounds",
                  "bake_off_decisions", "drift_events", "panel_decisions", "grading_queue"]:
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        log(f"  {table}: {n} rows")

    # Sample: cheap+capable models
    log("")
    log("=== Sample: top 10 cheap+capable models ===")
    cur = db.execute("""
        SELECT model_id, provider, input_per_1m_usd, output_per_1m_usd, context_window, tier
        FROM model_prices
        WHERE output_per_1m_usd > 0 AND output_per_1m_usd < 5
          AND provider IN ('anthropic', 'openai', 'google', 'deepseek', 'meta-llama', 'mistralai', 'qwen', 'moonshotai')
        ORDER BY output_per_1m_usd
        LIMIT 10
    """)
    for r in cur:
        log(f"  ${r['input_per_1m_usd']:.2f}/${r['output_per_1m_usd']:.2f}  [{r['tier']:7}]  {r['model_id']}  ctx={r['context_window']}")

    # Tiers distribution
    log("")
    log("=== Tier distribution in model_prices ===")
    cur = db.execute("SELECT tier, COUNT(*) c FROM model_prices GROUP BY tier ORDER BY c DESC")
    for r in cur:
        log(f"  {r['tier']:10}: {r['c']} models")

    db.close()
    log("Phase 0 complete.")

if __name__ == "__main__":
    main()
