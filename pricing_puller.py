"""Argos pricing puller - refreshes model_prices from OpenRouter every 6h.
Runs on garage, writes to argos.db.
Logs price changes >10% to ntfy.
"""
import sqlite3
import urllib.request
import urllib.parse
import json
import ssl
import time
import sys
import os

ARGOS_DB = "/home/andy/argos/argos.db"
NTFY_URL = "http://192.168.4.20:8090/homelab-alerts?auth=QmVhcmVyIHRrX3Z0Y21iMHEzZHlqejQyNHBmaHY3N2IxYnpoa29w"
LOG_PATH = "/home/andy/logs/argos-pricing-puller.log"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m, also_stdout=True):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    if also_stdout:
        print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def fetch_openrouter():
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "argos-pricing-puller/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.load(r).get("data", [])

def classify_tier(out_p):
    if out_p is None:
        return "unknown"
    if out_p >= 30:
        return "tier_1"
    if out_p >= 3:
        return "tier_2"
    return "tier_3"

def alert_ntfy(title, message, priority="default"):
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode(),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "moneybag",
                "User-Agent": "argos-pricing-puller/1.0"
            }
        )
        urllib.request.urlopen(req, timeout=10, context=ctx).read()
    except Exception as e:
        log(f"  ntfy alert failed: {e}")

def main():
    log("=== pricing puller start ===", also_stdout=True)

    try:
        models = fetch_openrouter()
        log(f"OpenRouter returned {len(models)} models")
    except Exception as e:
        log(f"FATAL: OpenRouter fetch failed: {e}")
        alert_ntfy("argos pricing puller FAILED", f"OpenRouter fetch failed: {e}", "high")
        sys.exit(1)

    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row

    # Cache existing prices for change detection
    existing = {r['model_id']: dict(r) for r in db.execute("""
        SELECT model_id, output_per_1m_usd, deprecated FROM model_prices
    """)}

    new_count = 0
    updated_count = 0
    deprecated_count = 0
    significant_changes = []

    seen_ids = set()
    for m in models:
        mid = m.get("id", "")
        if not mid:
            continue
        seen_ids.add(mid)
        provider = mid.split("/")[0] if "/" in mid else "unknown"
        pricing = m.get("pricing", {})
        try:
            in_p = float(pricing.get("prompt", "0")) * 1e6
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
        tier = classify_tier(out_p)

        # Check for significant price change
        if mid in existing:
            old_out = existing[mid].get("output_per_1m_usd")
            if old_out and out_p and abs(out_p - old_out) / old_out > 0.10:
                pct = (out_p - old_out) / old_out * 100
                significant_changes.append((mid, old_out, out_p, pct))
                updated_count += 1
        else:
            new_count += 1

        db.execute("""
            INSERT OR REPLACE INTO model_prices
            (model_id, provider, input_per_1m_usd, output_per_1m_usd, cached_input_per_1m_usd, request_overhead_usd,
             context_window, max_output_tokens, supports_tools, supports_json_schema, supports_vision,
             tier, deprecated, last_fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid, provider, in_p, out_p, cache_p, req_p,
            ctx_window, max_out, 0, 0, 1 if supports_vision else 0,
            tier, 0,
            time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(m)
        ))

    # Mark removed-from-OpenRouter models as deprecated
    for old_id in existing:
        if old_id not in seen_ids:
            db.execute("UPDATE model_prices SET deprecated = 1 WHERE model_id = ?", (old_id,))
            deprecated_count += 1

    db.commit()
    db.close()

    log(f"  {len(models)} models from OpenRouter")
    log(f"  {new_count} new, {updated_count} significant price changes")
    log(f"  {deprecated_count} marked deprecated (no longer on OpenRouter)")

    if significant_changes:
        log("")
        log("=== Significant price changes (>10%) ===")
        for mid, old, new, pct in significant_changes[:10]:
            log(f"  {mid}: ${old:.2f} -> ${new:.2f} ({pct:+.1f}%)")
        # Send ntfy summary
        msg_lines = [f"{len(significant_changes)} model(s) with >10% price change:"]
        for mid, old, new, pct in significant_changes[:5]:
            msg_lines.append(f"  {mid}: ${old:.2f} -> ${new:.2f} ({pct:+.1f}%)")
        if len(significant_changes) > 5:
            msg_lines.append(f"  ...+{len(significant_changes)-5} more")
        alert_ntfy("argos: model price changes", "\n".join(msg_lines), "default")

    log("=== pricing puller done ===")

if __name__ == "__main__":
    main()
