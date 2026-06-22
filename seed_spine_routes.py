#!/usr/bin/env python3
# seed_spine_routes.py - CHG-P9-048 (Argos Sprint 1)
# Seed one backend=spine route per LiteLLM-served, spine-ALLOWED_MODELS alias; delete the
# 5 dead-lineage pharos rows (their 3 alias-matched models are covered by the new spine routes;
# the 2 without a LiteLLM alias - gemma-4-31b, qwen3-coder - could not route via spine anyway).
# Idempotent UPSERT on routes (PK route_id). Transactional with a JSON backup. Verify-first.
import sqlite3, json, time, sys, os

DB = os.environ.get("ARGOS_DB", "/home/andy/argos/argos.db")
now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
ts = time.strftime("%Y%m%d-%H%M%S")

# The 19 spine-routable aliases (served by LiteLLM AND in spine ALLOWED_MODELS), with metadata.
# model_id MUST equal the LiteLLM alias (that is what /route-v2 returns and spine validates).
SPINE_MODELS = [
    # alias,                model_family,  notes
    ("deepseek-v3",         "deepseek",  "spine default; deepseek-v3.2 via LiteLLM"),
    ("kimi-k2",             "kimi",      "Moonshot Kimi K2 via LiteLLM"),
    ("kimi-k2.5",           "kimi",      "Moonshot Kimi K2.5 via LiteLLM"),
    ("gemini-flash",        "gemini",    "Google Gemini Flash via LiteLLM"),
    ("gemini-2.5-flash",    "gemini",    "Google Gemini 2.5 Flash via LiteLLM"),
    ("gpt-4o",              "gpt",       "OpenAI GPT-4o via LiteLLM"),
    ("claude-sonnet-46",    "claude",    "Anthropic Claude Sonnet 4.6 via LiteLLM"),
    ("claude-sonnet-or-45", "claude",    "Claude Sonnet 4.5 via OpenRouter/LiteLLM"),
    ("claude-haiku",        "claude",    "Anthropic Claude Haiku via LiteLLM"),
    ("or-auto",             "openrouter","OpenRouter auto-router via LiteLLM"),
    ("qwen3-cloud",         "qwen",      "Qwen3 cloud via LiteLLM"),
    ("mistral-large",       "mistral",   "Mistral Large via LiteLLM"),
    ("groq-llama-3.3-70b",  "llama",     "Groq Llama 3.3 70B via LiteLLM"),
    ("groq-llama-3.1-8b",   "llama",     "Groq Llama 3.1 8B via LiteLLM"),
    ("qwen2.5-14b",         "qwen",      "Qwen2.5 14B via LiteLLM"),
    ("mistral-small",       "mistral",   "Mistral Small via LiteLLM"),
    ("zdr-deepseek-v4-flash","deepseek", "ZDR DeepSeek v4 Flash (Renee-safe)"),
    ("zdr-gpt-oss-120b",    "gpt-oss",   "ZDR gpt-oss-120b (Renee-safe)"),
    ("zdr-llama-3.3-70b",   "llama",     "ZDR Llama 3.3 70B (Renee-safe)"),
]

def main():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; cur = c.cursor()

    # backup affected state
    bak = {"ts": ts, "pharos_rows_deleted": [], "spine_routes_before": []}
    bak["pharos_rows_deleted"] = [dict(r) for r in cur.execute("SELECT * FROM routes WHERE backend='pharos'").fetchall()]
    bak["spine_routes_before"] = [dict(r) for r in cur.execute("SELECT * FROM routes WHERE backend='spine'").fetchall()]
    open(f"/tmp/CHG-P9-048-backup-{ts}.json", "w").write(json.dumps(bak, indent=2, default=str))
    print(f"BACKUP: /tmp/CHG-P9-048-backup-{ts}.json ({len(bak['pharos_rows_deleted'])} pharos, {len(bak['spine_routes_before'])} existing spine)")

    # seed/upsert the 19 spine routes
    seeded = 0
    for alias, fam, note in SPINE_MODELS:
        route_id = f"spine:litellm:{alias}"
        cur.execute("""
            INSERT INTO routes(route_id, backend, tool, access_path, model_id, cost_mode, enabled,
                               notes, updated_at, model_family, execution_mode, healthcheck_type,
                               healthcheck_target, quota_bucket)
            VALUES(?, 'spine', 'litellm', 'litellm', ?, 'per_token', 1, ?, ?, ?, 'chat-completion',
                   'api-chat', ?, NULL)
            ON CONFLICT(route_id) DO UPDATE SET
                backend='spine', tool='litellm', access_path='litellm', model_id=excluded.model_id,
                cost_mode='per_token', enabled=1, notes=excluded.notes, updated_at=excluded.updated_at,
                model_family=excluded.model_family, execution_mode='chat-completion',
                healthcheck_type='api-chat', healthcheck_target=excluded.healthcheck_target
        """, (route_id, alias, f"CHG-P9-048 spine route. {note}", now, fam, alias))
        seeded += 1
    print(f"SEEDED/UPSERTED {seeded} spine routes")

    # delete the 5 pharos rows (and any orphan capacity rows for them)
    pharos_ids = [r["route_id"] for r in cur.execute("SELECT route_id FROM routes WHERE backend='pharos'").fetchall()]
    for pid in pharos_ids:
        cur.execute("DELETE FROM route_capacity WHERE route_id=?", (pid,))
    deld = cur.execute("DELETE FROM routes WHERE backend='pharos'").rowcount
    print(f"DELETED {deld} pharos routes: {pharos_ids}")

    c.commit()

    # verify
    n_spine = cur.execute("SELECT COUNT(*) FROM routes WHERE backend='spine'").fetchone()[0]
    n_pharos = cur.execute("SELECT COUNT(*) FROM routes WHERE backend='pharos'").fetchone()[0]
    n_total = cur.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
    print(f"VERIFY: spine={n_spine} pharos={n_pharos} total_routes={n_total}")
    # sanity: every spine route's model_id is non-null
    nullmodel = cur.execute("SELECT COUNT(*) FROM routes WHERE backend='spine' AND (model_id IS NULL OR model_id='')").fetchone()[0]
    print(f"VERIFY: spine routes with null model_id = {nullmodel} (must be 0)")
    c.close()

if __name__ == "__main__":
    main()
