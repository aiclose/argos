"""LiteLLM model audit + reconciliation against argos.db.model_prices.

Adds a `litellm_alias` column so Argos knows the LiteLLM model_name to use when routing.
Also inserts local Ollama models as synthetic $0 entries.
"""
import sqlite3
import re
import sys
import os
import yaml

ARGOS_DB = "/home/andy/argos/argos.db"
LITELLM_CONFIG = "/tmp/litellm_config.yaml"  # copied from UM780

def parse_litellm_config(path):
    """Parse model list from config.yaml."""
    with open(path) as f:
        data = yaml.safe_load(f)
    models = data.get("model_list", [])
    aliases = []  # list of (litellm_name, underlying_provider, underlying_model)
    for m in models:
        name = m.get("model_name", "")
        params = m.get("litellm_params", {})
        underlying = params.get("model", "")
        # underlying can be: openai/<openrouter-id>, anthropic/<id>, ollama/<id>
        if "/" in underlying:
            prefix, rest = underlying.split("/", 1)
        else:
            prefix, rest = "", underlying
        aliases.append({
            "litellm_name": name,
            "prefix": prefix,
            "underlying": rest,
            "raw": underlying,
        })
    return aliases

def main():
    if not os.path.exists(LITELLM_CONFIG):
        print(f"FATAL: {LITELLM_CONFIG} missing")
        sys.exit(1)

    aliases = parse_litellm_config(LITELLM_CONFIG)
    print(f"LiteLLM config has {len(aliases)} model_name entries:")
    for a in aliases:
        print(f"  {a['litellm_name']:25} -> {a['raw']}")
    print()

    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row

    # Add litellm_alias column if not present
    cols = [r['name'] for r in db.execute("PRAGMA table_info(model_prices)")]
    if "litellm_alias" not in cols:
        print("Adding litellm_alias column to model_prices")
        db.execute("ALTER TABLE model_prices ADD COLUMN litellm_alias TEXT")
        db.commit()
    else:
        print("litellm_alias column already exists")

    # For each LiteLLM alias whose underlying is an OpenRouter id (prefix=openai means via OpenRouter),
    # find the matching model_id in argos.db.model_prices and update litellm_alias.
    # Also handle direct anthropic/x mappings.
    matched = 0
    or_aliases = {}  # underlying-without-prefix -> litellm_name
    direct_aliases = {}  # underlying with provider/model -> litellm_name
    ollama_models = []

    for a in aliases:
        prefix = a["prefix"]
        underlying = a["underlying"]
        litellm_name = a["litellm_name"]

        if prefix == "openai":
            # openai/<openrouter-style-id> e.g. openai/anthropic/claude-haiku-4-5
            or_aliases[underlying] = litellm_name
        elif prefix == "anthropic":
            # direct: anthropic/<model> - map to OpenRouter id "anthropic/<model>"
            direct_aliases[f"anthropic/{underlying}"] = litellm_name
        elif prefix == "ollama":
            # local model - add as synthetic entry
            ollama_models.append((litellm_name, underlying))
        elif prefix == "":
            # unprefixed - skip
            continue
        else:
            # unknown
            print(f"  skipping unknown prefix: {a['raw']}")

    # First pass: openai/X mappings (via OpenRouter)
    for underlying, name in or_aliases.items():
        # Try direct match
        cur = db.execute("SELECT model_id FROM model_prices WHERE model_id = ?", (underlying,))
        row = cur.fetchone()
        if row:
            db.execute("UPDATE model_prices SET litellm_alias = ? WHERE model_id = ?",
                       (name, underlying))
            matched += 1
            continue
        # Try without version tail (e.g., claude-haiku-4-5 vs claude-haiku-4.5)
        normalized = underlying.replace("-4-5", "-4.5").replace("-4-7", "-4.7")
        cur = db.execute("SELECT model_id FROM model_prices WHERE model_id = ?", (normalized,))
        row = cur.fetchone()
        if row:
            db.execute("UPDATE model_prices SET litellm_alias = ? WHERE model_id = ?",
                       (name, normalized))
            matched += 1
            continue
        print(f"  WARN: no match for {underlying} (litellm name: {name})")

    # Second pass: direct anthropic/x mappings (these may overlap with OR but flag both)
    for or_id, name in direct_aliases.items():
        normalized = or_id.replace("-4-5", "-4.5").replace("-4-7", "-4.7")
        cur = db.execute("SELECT model_id, litellm_alias FROM model_prices WHERE model_id = ?", (normalized,))
        row = cur.fetchone()
        if row:
            existing = row['litellm_alias']
            if existing and existing != name:
                # Already aliased via OR; keep the OR one but note direct exists
                print(f"  {normalized}: keeping {existing} (also has direct: {name})")
            else:
                db.execute("UPDATE model_prices SET litellm_alias = ? WHERE model_id = ?",
                           (name, normalized))
                matched += 1

    # Third: insert ollama models as synthetic entries
    for name, underlying in ollama_models:
        synthetic_id = f"ollama/{underlying}"
        db.execute("""
            INSERT OR REPLACE INTO model_prices
            (model_id, provider, input_per_1m_usd, output_per_1m_usd, request_overhead_usd,
             context_window, supports_tools, supports_json_schema, supports_vision,
             tier, deprecated, litellm_alias, last_fetched_at, raw_json)
            VALUES (?, 'ollama', 0, 0, 0, 32768, 0, 0, 0, 'tier_3', 0, ?,
                    datetime('now'), ?)
        """, (synthetic_id, name, '{"provider":"ollama","local":true}'))
    
    db.commit()

    print(f"\nMatched {matched} / 367 OpenRouter models to LiteLLM aliases")
    print(f"Inserted {len(ollama_models)} local Ollama synthetic entries")
    print()

    # Summary
    print("=== Summary ===")
    n_total = db.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0]
    n_aliased = db.execute("SELECT COUNT(*) FROM model_prices WHERE litellm_alias IS NOT NULL").fetchone()[0]
    print(f"  Total models in argos.db: {n_total}")
    print(f"  Reachable via LiteLLM:    {n_aliased} ({n_aliased/n_total*100:.1f}%)")
    print(f"  OpenRouter-only:          {n_total - n_aliased - len(ollama_models)} (need to use direct OR for these)")
    print()
    print("Models reachable via LiteLLM:")
    cur = db.execute("""
        SELECT model_id, litellm_alias, tier, output_per_1m_usd
        FROM model_prices
        WHERE litellm_alias IS NOT NULL
        ORDER BY output_per_1m_usd ASC NULLS LAST
    """)
    for r in cur:
        print(f"  ${(r['output_per_1m_usd'] or 0):>6.2f}/1M out  [{r['tier']:7}]  {r['model_id']:50} -> {r['litellm_alias']}")

    db.close()

if __name__ == "__main__":
    main()
