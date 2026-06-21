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

def reconcile_routes(db, seen_ids):
    """Auto-disable enabled routes whose backing OpenRouter model is gone/deprecated.

    A route is considered dead when its model_id is NON-NULL and any of:
      - model_id absent from model_prices, OR
      - model_prices.deprecated = 1, OR
      - model_id not seen in this OpenRouter fetch (not in seen_ids).

    Behaviour / guarantees:
      - NEVER touches routes with model_id IS NULL: those are non-OpenRouter
        backends (codex-cli, claude-code, ...) and have no OpenRouter model to check.
      - REACTIVATION GUARD: only ever flips enabled 1 -> 0, NEVER 0 -> 1. If a
        model reappears on OpenRouter a human must re-enable the route deliberately;
        auto-re-enabling could resurrect a route the operator disabled for other
        reasons. We therefore only SELECT enabled=1 routes here.
      - Idempotent: already-disabled routes are left untouched, so notes are never
        duplicated and a second run disables nothing new.
      - Appends (does not clobber) a dated note to the existing notes column.

    Returns a list of (route_id, model_id) tuples disabled this run.
    """
    disabled = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    today = time.strftime("%Y-%m-%d")
    note_suffix = f" [auto-disabled {today}: model deprecated/absent on OpenRouter]"

    # Live = present in model_prices AND not deprecated. (Covers both the
    # "absent from model_prices" and "deprecated=1" conditions in one set test.)
    live = {r["model_id"] for r in db.execute(
        "SELECT model_id FROM model_prices WHERE deprecated = 0"
    )}

    # Only currently-enabled, OpenRouter-backed routes are candidates.
    rows = db.execute("""
        SELECT route_id, model_id, notes FROM routes
        WHERE model_id IS NOT NULL AND enabled = 1
    """).fetchall()

    for row in rows:
        mid = row["model_id"]
        dead = (mid not in live) or (mid not in seen_ids)
        if not dead:
            continue
        new_notes = (row["notes"] or "") + note_suffix
        db.execute(
            "UPDATE routes SET enabled = 0, notes = ?, updated_at = ? WHERE route_id = ?",
            (new_notes, now, row["route_id"])
        )
        disabled.append((row["route_id"], mid))

    return disabled

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
        sp = m.get("supported_parameters") or []
        supports_tools = 1 if "tools" in sp else 0
        supports_json_schema = 1 if ("structured_outputs" in sp or "response_format" in sp) else 0
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
            ctx_window, max_out, supports_tools, supports_json_schema, 1 if supports_vision else 0,
            tier, 0,
            time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(m)
        ))

    # Mark removed-from-OpenRouter models as deprecated
    for old_id in existing:
        if old_id not in seen_ids:
            db.execute("UPDATE model_prices SET deprecated = 1 WHERE model_id = ?", (old_id,))
            deprecated_count += 1

    # Reconcile routes table against the freshly-updated model catalogue.
    # Best-effort: a failure here must NOT abort the pricing run.
    disabled_routes = []
    try:
        disabled_routes = reconcile_routes(db, seen_ids)
    except Exception as e:
        log(f"  route reconciliation failed (non-fatal): {e}")

    db.commit()
    db.close()

    log(f"  {len(models)} models from OpenRouter")
    log(f"  {new_count} new, {updated_count} significant price changes")
    log(f"  {deprecated_count} marked deprecated (no longer on OpenRouter)")
    log(f"  {len(disabled_routes)} routes auto-disabled (model deprecated/absent)")
    if disabled_routes:
        for rid, mid in disabled_routes:
            log(f"    disabled route {rid} -> {mid}")
        msg_lines = [f"{len(disabled_routes)} route(s) auto-disabled (model deprecated/absent on OpenRouter):"]
        msg_lines += [f"  {rid} ({mid})" for rid, mid in disabled_routes]
        alert_ntfy("argos: routes auto-disabled", "\n".join(msg_lines), "high")

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
