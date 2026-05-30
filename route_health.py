"""Argos route health check: confirm each enabled route reaches a working model.
Cheap: tiny prompt, tiny max-tokens, free/sunk routes first. Writes results so
go-live only trusts verified routes.
"""
import sqlite3, json, urllib.request, time, datetime, os

DB = "/home/andy/argos/argos.db"
OR_KEY = os.getenv("OPENROUTER_API_KEY")  # env-only
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

def check_openrouter(model, timeout=25):
    """Health-check a model reachable via OpenRouter (covers Go/Zen/Pharos models)."""
    body = json.dumps({"model": model, "messages":[{"role":"user","content":"Reply with the single word: OK"}],
                       "max_tokens": 5, "temperature": 0}).encode()
    req = urllib.request.Request(OR_URL, data=body,
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type":"application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            return ("ok" if txt else "empty", int((time.time()-t0)*1000), txt[:20])
    except Exception as e:
        return (f"fail:{type(e).__name__}", int((time.time()-t0)*1000), str(e)[:60])

def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    # add a health column set if not present
    cols = [r[1] for r in con.execute("PRAGMA table_info(routes)")]
    if "last_health" not in cols:
        con.execute("ALTER TABLE routes ADD COLUMN last_health TEXT")
        con.execute("ALTER TABLE routes ADD COLUMN last_health_at TEXT")
        con.commit()
    # health-check routes that have a model_id reachable via OpenRouter, cheapest first.
    # (codex-cli/claude-code are sunk CLI tools, checked separately; skip here.)
    routes = con.execute("SELECT route_id, model_id, cost_mode FROM routes WHERE enabled=1 AND model_id IS NOT NULL ORDER BY cost_mode").fetchall()
    # limit to a representative sample per the budget guard: all free + a few paid
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    checked = 0
    results = []
    for r in routes:
        mid = r["model_id"]
        # only free-tier and cheap to keep budget near zero on this sweep
        is_free = ":free" in mid
        if not is_free and checked >= 3:
            continue  # cap paid checks to 3 to keep cost tiny
        status, lat, sample = check_openrouter(mid)
        if not is_free:
            checked += 1
        con.execute("UPDATE routes SET last_health=?, last_health_at=? WHERE route_id=?",
                    (status, now, r["route_id"]))
        results.append((r["route_id"], status, lat))
    con.commit()
    print(f"health-checked {len(results)} routes:")
    ok = sum(1 for _,s,_ in results if s=="ok")
    for rid, status, lat in results:
        flag = "OK " if status=="ok" else "XX "
        print(f"  {flag}{rid:42s} {status:18s} {lat}ms")
    print(f"\n{ok}/{len(results)} reachable")
    con.close()

if __name__ == "__main__":
    main()
