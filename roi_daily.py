"""Daily Argos ROI summary - 'Argos would have saved/spent $X yesterday'.
Cron: 55 23 * * * (5 min before midnight)
"""
import sqlite3, os, time, json, urllib.request, urllib.parse, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
LOG = "/home/andy/logs/argos-roi-daily.log"
ARGOS_DB = "/home/andy/argos/argos.db"
COST_LOG_REMOTE = "andy@192.168.4.10:/home/andy/orchestrator/cost-log.db"
COST_LOG_LOCAL = "/tmp/cost_log_for_roi.db"

def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

# scp cost_log
rc = os.system(f"scp -q {COST_LOG_REMOTE} {COST_LOG_LOCAL} 2>/dev/null")
if rc != 0:
    log(f"scp failed rc={rc}")
    raise SystemExit(1)

yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))

db_cl = sqlite3.connect(COST_LOG_LOCAL, timeout=30)
db_cl.row_factory = sqlite3.Row
cl_rows = list(db_cl.execute(
    "SELECT id, tag, model, cost_usd, status FROM cost_log WHERE date(ts) = ?", (yesterday,)
))
db_cl.close()

actual_total = sum(r['cost_usd'] or 0 for r in cl_rows)
n_dispatches = len(cl_rows)

if n_dispatches == 0:
    log(f"no dispatches yesterday ({yesterday})")
    raise SystemExit(0)

db_a = sqlite3.connect(ARGOS_DB, timeout=30)
db_a.row_factory = sqlite3.Row

predicted_total = 0.0
matched = 0
unmatched = 0
top_savings = []
top_costlier = []
for r in cl_rows:
    actual = r['cost_usd'] or 0
    dispatch_id = f"costlog-{r['id']}"
    pred_row = db_a.execute(
        "SELECT predicted_cost_p50, selected_model_id FROM predictions WHERE dispatch_id = ? ORDER BY prediction_id DESC LIMIT 1",
        (dispatch_id,)
    ).fetchone()
    if pred_row and pred_row['predicted_cost_p50'] is not None:
        pred = pred_row['predicted_cost_p50']
        if pred < 0:
            unmatched += 1
            continue
        predicted_total += pred
        matched += 1
        delta = actual - pred
        if delta > 0.01:
            top_savings.append((r['tag'], pred, actual, delta, pred_row['selected_model_id']))
        elif delta < -0.01:
            top_costlier.append((r['tag'], pred, actual, delta, pred_row['selected_model_id']))
    else:
        unmatched += 1

db_a.close()

delta_total = actual_total - predicted_total
verdict = "✅ Argos cheaper" if delta_total > 0.01 else "🔴 Argos costlier" if delta_total < -0.01 else "≈ same"

top_savings.sort(key=lambda x: -x[3])
top_costlier.sort(key=lambda x: x[3])

msg_lines = [
    f"📊 ARGOS DAILY ROI - {yesterday}",
    "",
    f"Dispatches: {n_dispatches} (matched {matched}, unmatched {unmatched})",
    f"Actual total: ${actual_total:.4f}",
    f"Argos predicted: ${predicted_total:.4f}",
    f"Delta: ${delta_total:+.4f}  {verdict}",
    "",
]
if top_savings:
    msg_lines.append("Top potential savings (Argos picked cheaper):")
    for tag, pred, actual, delta, mdl in top_savings[:3]:
        tag_disp = (tag or "?")[:30]
        mdl_disp = (mdl or "?")[:30]
        msg_lines.append(f"  • {tag_disp}: actual ${actual:.4f} → Argos ${pred:.4f} ({mdl_disp})")
if top_costlier:
    msg_lines.append("")
    msg_lines.append("Top regrets (Argos would have cost more):")
    for tag, pred, actual, delta, mdl in top_costlier[:3]:
        tag_disp = (tag or "?")[:30]
        mdl_disp = (mdl or "?")[:30]
        msg_lines.append(f"  • {tag_disp}: actual ${actual:.4f} ← Argos ${pred:.4f} ({mdl_disp})")

msg = "\n".join(msg_lines)
log(msg)

data = urllib.parse.urlencode({"chat_id":"8796667560","text":msg}).encode()
req = urllib.request.Request("https://api.telegram.org/bot8260412478:AAFKKD0knEhEorQ08__ZDIlSJTmngJ-FakY/sendMessage",
    data=data, headers={"User-Agent":"curl/8.0"})
try:
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        log(f"telegram sent")
except Exception as e:
    log(f"telegram failed: {e}")
