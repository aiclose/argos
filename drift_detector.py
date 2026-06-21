"""Argos drift detector - daily cron on garage.

Phase 4+ will compare predicted_quality vs actual; for now it does what's possible
with available data:
  1. Pricing freshness: alert if model_prices last_fetched > 12h ago
  2. Tier shift: alert if any model used in dispatches changed tier vs registry
  3. Failure rate: alert if today's failure rate >25%
  4. Backfill predictions check: count when ready
"""
import sqlite3
import urllib.request
import urllib.parse
import json
import ssl
import time
import sys
import os
from datetime import datetime, timedelta, timezone

ARGOS_DB = "/home/andy/argos/argos.db"
NTFY_URL = "http://192.168.4.20:8090/homelab-alerts?auth=QmVhcmVyIHRrX3Z0Y21iMHEzZHlqejQyNHBmaHY3N2IxYnpoa29w"
LOG_PATH = "/home/andy/logs/argos-drift-detector.log"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def alert_ntfy(title, message, priority="default", tags="rotating_light"):
    try:
        req = urllib.request.Request(
            NTFY_URL, data=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags,
                     "User-Agent": "argos-drift/1.0"})
        urllib.request.urlopen(req, timeout=10, context=ctx).read()
    except Exception as e:
        log(f"  ntfy failed: {e}")

def record_event(db, model_id, event_type, magnitude_pct, action):
    db.execute("""
        INSERT INTO drift_events (model_id, event_type, magnitude_pct, started_at, action_taken)
        VALUES (?, ?, ?, ?, ?)
    """, (model_id, event_type, magnitude_pct, time.strftime("%Y-%m-%d %H:%M:%S"), action))
    db.commit()

def check_pricing_freshness(db):
    """Alert if last pricing puller run was >12h ago."""
    row = db.execute("""
        SELECT MAX(last_fetched_at) latest FROM model_prices
    """).fetchone()
    latest = row[0]
    if not latest:
        log("  WARN: no pricing data at all")
        return False
    
    try:
        latest_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        log(f"  could not parse pricing timestamp: {latest}")
        return False

    age = datetime.now() - latest_dt
    age_hours = age.total_seconds() / 3600

    if age_hours > 12:
        log(f"  STALE: pricing data is {age_hours:.1f}h old (last: {latest})")
        alert_ntfy(
            "argos drift: pricing puller stale",
            f"Last fetch was {age_hours:.1f}h ago. Cron may have failed.",
            priority="default", tags="warning"
        )
        return False
    else:
        log(f"  OK: pricing data is {age_hours:.1f}h old")
        return True

def check_failure_rate(db):
    """Alert if today's dispatch failure rate >25%."""
    row = db.execute("""
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN status LIKE 'failed%' OR status = 'errored' THEN 1 ELSE 0 END) failed
        FROM dispatches
        WHERE date(ts) = date('now')
    """).fetchone()
    
    if row[0] == 0:
        log(f"  no dispatches today, skipping failure rate check")
        return True
    
    rate = row[1] / row[0]
    log(f"  today: {row[1]}/{row[0]} failed ({rate*100:.1f}%)")
    
    if rate > 0.25:
        alert_ntfy(
            "argos drift: failure rate spike",
            f"Today's failure rate: {rate*100:.1f}% ({row[1]}/{row[0]} failed)",
            priority="high", tags="rotating_light"
        )
        return False
    return True

def check_tier_shifts(db):
    """Alert if any active model has shifted tier significantly."""
    # We don't track historical tier per model yet; for now this is a stub
    # that will grow once we have time-series pricing snapshots.
    log("  tier shift check: stub (no historical data yet)")
    return True

def check_predictions_status(db):
    """Report how many predictions exist (Phase 4+ will use this)."""
    n_dispatches = db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    n_predictions = db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    log(f"  predictions coverage: {n_predictions}/{n_dispatches} dispatches have predictions")
    
    if n_predictions == 0:
        log("  (Phase 4+ will populate predictions; quality drift comparison not active yet)")
    elif n_predictions > 0:
        # Future: actual drift comparison
        log("  predictions exist - quality drift comparison will activate when we have ≥30 paired data points")

def check_classification_coverage(db):
    """Report how many dispatches have task_class set."""
    row = db.execute("""
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN task_class IS NOT NULL THEN 1 ELSE 0 END) classified
        FROM dispatches
    """).fetchone()
    if row[0] == 0:
        return
    pct = row[1] / row[0] * 100
    log(f"  classification: {row[1]}/{row[0]} ({pct:.1f}%) have task_class")


def check_quality_demote(db):
    """#669: demote models showing >=10% quality under predicted over last 50 dispatches.
    
    For each model with at least 50 dispatches having quality_score AND
    matching prediction's predicted_quality, compute avg actual - avg predicted.
    If avg_actual is more than 10% below avg_predicted, mark model_prices.deprecated=1
    and ntfy.
    """
    log("[5.6] check_quality_demote")
    rows = db.execute("""
        SELECT d.model_used,
               COUNT(*) as n,
               AVG(d.quality_score) as avg_actual,
               AVG(p.predicted_quality) as avg_predicted
        FROM dispatches d
        JOIN predictions p ON p.dispatch_id = d.dispatch_id
        WHERE d.quality_score IS NOT NULL
          AND p.predicted_quality IS NOT NULL
          AND d.ts >= datetime('now', '-30 days')
        GROUP BY d.model_used
        HAVING n >= 50
    """).fetchall()
    
    if not rows:
        log("  no models have 50+ scored dispatches yet (returning)")
        return 0
    
    demoted = 0
    for model_used, n, avg_actual, avg_predicted in rows:
        if avg_predicted is None or avg_predicted == 0:
            continue
        delta_pct = (avg_actual - avg_predicted) / avg_predicted
        log(f"  {model_used}: n={n} avg_actual={avg_actual:.3f} avg_predicted={avg_predicted:.3f} delta_pct={delta_pct*100:.1f}%")
        if delta_pct < -0.10:
            # Already deprecated? Skip to avoid duplicate alerts
            already = db.execute(
                "SELECT deprecated FROM model_prices WHERE model_id=?", (model_used,)
            ).fetchone()
            if already and already[0]:
                log(f"    already deprecated, skipping")
                continue
            # Demote
            db.execute("UPDATE model_prices SET deprecated=1 WHERE model_id=?", (model_used,))
            db.commit()
            demoted += 1
            try:
                record_event(db, model_used, 'quality_demoted', delta_pct * 100, 'set deprecated=1')
            except Exception as e:
                log(f"    record_event failed: {e}")
            alert_ntfy(
                f"Argos demoted {model_used}",
                f"Quality {delta_pct*100:.1f}% under predicted over {n} dispatches. Marked deprecated=1.",
                priority="high", tags="warning",
            )
            log(f"    DEMOTED {model_used}")
    
    log(f"  demoted: {demoted}")
    return demoted

def main():
    log("=== drift detector start ===")
    
    if not os.path.exists(ARGOS_DB):
        log(f"FATAL: {ARGOS_DB} missing")
        sys.exit(1)

    db = sqlite3.connect(ARGOS_DB)
    db.row_factory = sqlite3.Row
    
    log("[1] pricing freshness check")
    check_pricing_freshness(db)
    
    log("[2] failure rate check")
    check_failure_rate(db)
    
    log("[3] tier shift check")
    check_tier_shifts(db)
    
    log("[4] predictions coverage")
    check_predictions_status(db)
    
    log("[5] classification coverage")
    check_classification_coverage(db)
    
    check_quality_demote(db)
    db.close()
    # [5.5] shadow drift check (predicted vs actual cost ratio per task_class)
    log("[5.5] shadow drift check")
    try:
        def _tg_send(text):
            try:
                import urllib.request, urllib.parse, ssl
                ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
                data = urllib.parse.urlencode({"chat_id":"8796667560","text":text}).encode()
                req = urllib.request.Request("https://api.telegram.org/bot8260412478:AAFKKD0knEhEorQ08__ZDIlSJTmngJ-FakY/sendMessage", data=data, headers={"User-Agent":"curl/8.0"})
                urllib.request.urlopen(req, timeout=10, context=ctx)
            except Exception as e:
                log(f"  telegram failed: {e}")
        import sqlite3 as _sqlite3
        _db = _sqlite3.connect(ARGOS_DB)
        _db.row_factory = _sqlite3.Row
        drifted = check_shadow_drift(_db, _tg_send)
        _db.close()
        if drifted:
            log(f"  {len(drifted)} task_class(es) drifted; alert sent")
        else:
            log("  no shadow drift detected")
    except Exception as e:
        log(f"  shadow drift check error: {e}")

    log("=== drift detector done ===")

# call new check

def check_shadow_drift(db, telegram_send=None):
    """Per task_class, ratio of predicted vs actual cost in last 24h.
    Alert if ratio > 1.5 or < 0.67 with N >= 20 dispatches in that class.
    """
    cur = db.execute("""
        SELECT 
            d.task_class,
            COUNT(*) n,
            SUM(p.predicted_cost_p50) sum_pred,
            SUM(d.actual_cost_usd) sum_actual
        FROM dispatches d
        JOIN predictions p ON d.dispatch_id = p.dispatch_id
        WHERE d.ts > datetime('now', '-1 day')
          AND p.predicted_cost_p50 > 0
          AND d.actual_cost_usd >= 0
        GROUP BY d.task_class
        HAVING n >= 20
    """)
    drifted = []
    for r in cur:
        pred = r["sum_pred"] or 0
        actual = r["sum_actual"] or 0
        if actual <= 0:
            continue
        ratio = pred / actual
        if ratio > 1.5 or ratio < 0.67:
            drifted.append((r["task_class"], r["n"], pred, actual, ratio))
    if drifted:
        msg = "ARGOS SHADOW DRIFT (last 24h):\n"
        for tc, n, pred, actual, ratio in drifted:
            msg += f"  {tc}: n={n} pred=${pred:.4f} actual=${actual:.4f} ratio={ratio:.2f}\n"
        msg += "\nConsider retraining or adjusting argos-rules.yaml"
        if telegram_send:
            telegram_send(msg)
        print(msg)
        return drifted
    print("[shadow drift] no drift detected (need n>=20 per class)")
    return []

if __name__ == "__main__":
    main()
