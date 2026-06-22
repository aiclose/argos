#!/usr/bin/env python3
# argos_price_quota_pull.py - daily: refresh model_prices from OpenRouter + derive free-lane quota caps.
import sqlite3, json, time, urllib.request, sys, os
DB="/home/andy/argos/argos.db"
KEYFILE="/home/andy/argos/.or_key"
KEY=open(KEYFILE).read().strip() if os.path.exists(KEYFILE) else os.getenv("OPENROUTER_API_KEY","")
now=time.strftime("%Y-%m-%d %H:%M:%S")
def log(m): print(f"[{now}] {m}")

# 1. fetch models
req=urllib.request.Request("https://openrouter.ai/api/v1/models",
    headers={"Authorization":"Bearer "+KEY,"User-Agent":"curl/8.0"})
models=json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]
log(f"fetched {len(models)} models")

# CHG-P9-050: notional daily-requests ration for the flat-rate Max OAuth codex
# bucket. There is NO real cap to pull (flat-rate subscription), so codex-oauth's
# quota_caps row would otherwise stay all-NULL and the bucket shadow price would
# never ramp - the replay phantom. This gives it a tunable notional cap so codex
# is ~free until it nears a sustainable daily volume, then spills. One-line tunable.
CODEX_NOTIONAL_DAILY_REQUESTS = 200

c=sqlite3.connect(DB)
upd=0
for m in models:
    mid=m["id"]; pr=m.get("pricing",{}) or {}
    # OpenRouter variable-price meta-models (auto/fusion/etc) report price "-1"; any
    # negative is "unknown/variable", NOT free. Clamp to a conservative p90 upper bound
    # (input $3 / output $15 per 1M) so they stay routable but never look cheapest. CHG-P9-047.
    EST_IN_PER_1M, EST_OUT_PER_1M = 3.0, 15.0
    try:
        inp=float(pr.get("prompt") or 0)*1_000_000
        out=float(pr.get("completion") or 0)*1_000_000
    except (TypeError, ValueError):
        inp=out=0.0
    if inp < 0: inp = EST_IN_PER_1M
    if out < 0: out = EST_OUT_PER_1M
    tp=m.get("top_provider",{}) or {}
    c.execute("""INSERT INTO model_prices(model_id,provider,input_per_1m_usd,output_per_1m_usd,context_window,max_output_tokens,last_fetched_at,raw_json,deprecated)
                 VALUES(?,?,?,?,?,?,?,?,0)
                 ON CONFLICT(model_id) DO UPDATE SET input_per_1m_usd=excluded.input_per_1m_usd,output_per_1m_usd=excluded.output_per_1m_usd,
                   context_window=excluded.context_window,max_output_tokens=excluded.max_output_tokens,last_fetched_at=excluded.last_fetched_at,raw_json=excluded.raw_json,deprecated=0""",
              (mid, mid.split("/")[0], inp, out, m.get("context_length"), tp.get("max_completion_tokens"), now, json.dumps(m)))
    upd+=1
log(f"upserted {upd} prices")

# 2. derive free-lane caps: for each enabled :free route, read per_request_limits from its model raw_json; aggregate per bucket (min of limits as conservative cap)
price_raw={r[0]:r[1] for r in c.execute("SELECT model_id,raw_json FROM model_prices")}
bucket_free_limits={}
for route_id,model_id,bucket in c.execute("SELECT route_id,model_id,quota_bucket FROM routes WHERE enabled=1 AND model_id LIKE '%:free' AND quota_bucket IS NOT NULL"):
    rj=price_raw.get(model_id)
    if not rj: continue
    try: prl=(json.loads(rj).get("per_request_limits") or {})
    except: prl={}
    # OpenRouter free tier daily request cap is account-level (~50-1000); per_request_limits rarely set. Track tokens-per-request if present.
    bucket_free_limits.setdefault(bucket,{"models":0})
    bucket_free_limits[bucket]["models"]+=1

# OpenRouter free-tier policy: shared daily request budget per account (not per model).
# Conservative: 1000 free req/day shared across all :free lanes in a bucket (override via env).
FREE_REQ_BUDGET=int(os.getenv("OR_FREE_DAILY_REQ","1000"))
for bucket,info in bucket_free_limits.items():
    c.execute("UPDATE quota_caps SET daily_requests_cap=?, source=?, updated_at=? WHERE bucket=?",
              (FREE_REQ_BUDGET, f"daily-pull: {info['models']} free models, OR shared free budget", now, bucket))
    log(f"bucket {bucket}: {info['models']} free models, cap {FREE_REQ_BUDGET} req/day")

# 3. maintain the codex-oauth notional ration so a pull never resets it to NULL.
# INSERT-or-UPDATE (row currently exists, but stay robust if it is ever absent).
# Touches ONLY codex-oauth - claude-oauth (retired) and deepseek-direct are left as is.
CODEX_SOURCE = "notional Max-OAuth rationing cap (no real cap; tunable)"
cur = c.execute(
    "UPDATE quota_caps SET daily_requests_cap=?, source=?, updated_at=? WHERE bucket='codex-oauth'",
    (CODEX_NOTIONAL_DAILY_REQUESTS, CODEX_SOURCE, now))
if cur.rowcount == 0:
    c.execute(
        "INSERT INTO quota_caps(bucket, daily_requests_cap, source, updated_at) VALUES('codex-oauth',?,?,?)",
        (CODEX_NOTIONAL_DAILY_REQUESTS, CODEX_SOURCE, now))
log(f"bucket codex-oauth: notional cap {CODEX_NOTIONAL_DAILY_REQUESTS} req/day (no real cap; flat-rate Max OAuth)")

c.commit()
log("done")
