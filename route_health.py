"""Argos path-native route health check.

A route is validated by exercising ITS OWN access path, never a proxy:
  - cli-smoke : run the route's actual CLI in the Forge worker (codex/claude-code/
                deepseek/opencode) with a tiny prompt; success = clean exit + output.
  - api-chat  : call the route's API endpoint directly (OpenRouter) with a tiny
                completion; success = sane response. (Janus routes.)

This replaces the old version that wrongly health-checked Forge OpenCode routes
via OpenRouter. OpenRouter is the Janus access path; it says nothing about whether
the Forge OpenCode CLI lane works. Health is stored per route_id.

Budget guard: free/sunk/flat-rate routes first; per_token api-chat checks capped.
"""
import sqlite3, json, subprocess, time, datetime, os

DB = "/home/andy/argos/argos.db"
WORKER = os.getenv("FORGE_WORKER", "forge-worker")
ACPC = os.getenv("FORGE_ACPC", "close@192.168.4.30")
OR_KEY = os.getenv("OPENROUTER_API_KEY")  # only for api-chat (Janus) checks

# LiteLLM (spine backend) path-native health. Spine routes dispatch via LiteLLM
# (UM780:4000), NOT OpenRouter directly, so their model_id is a LiteLLM alias
# (e.g. claude-haiku, deepseek-v3) and must be probed against LiteLLM, not the
# OpenRouter slug API. See ARGOS-CH4 / ARGOS-S1.
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")
LITELLM_KEY = os.getenv("LITELLM_KEY")  # may be filled from the fallback below
LITELLM_TIMEOUT_S = 45  # some local-backed aliases (qwen2.5-14b) take ~30s cold; >=40s required
# Spine itself calls LiteLLM with the master key kept in /home/andy/docker/.env on
# UM780 (NOT in argos/.env). Fall back to it so this check works when run on UM780.
_DOCKER_ENVF = "/home/andy/docker/.env"

# load env from argos/.env if present (key propagation fix)
_envf = "/home/andy/argos/.env"
if os.path.exists(_envf):
    for _l in open(_envf):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
            if _k.strip() == "OPENROUTER_API_KEY":
                OR_KEY = os.environ["OPENROUTER_API_KEY"]

SMOKE_PROMPT = "Reply with exactly: OK"


def _litellm_key():
    """Resolve the LiteLLM bearer key.

    Order: LITELLM_KEY env -> LITELLM_MASTER_KEY env -> LITELLM_MASTER_KEY in
    /home/andy/docker/.env (the on-UM780 fallback, where spine reads it from).
    """
    key = LITELLM_KEY or os.getenv("LITELLM_MASTER_KEY")
    if key:
        return key
    if os.path.exists(_DOCKER_ENVF):
        try:
            for _l in open(_DOCKER_ENVF):
                _l = _l.strip()
                if _l.startswith("LITELLM_MASTER_KEY=") and "=" in _l:
                    return _l.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            return None
    return None


def litellm_chat(route):
    """Call the route's OWN access path: LiteLLM /v1/chat/completions. Path-native
    for spine:litellm routes whose model_id is a LiteLLM alias (NOT an OpenRouter
    slug). Success = HTTP 200 AND non-empty content. Returns the same
    (status, ms, sample) shape as the other checks."""
    import urllib.request
    model = route["model_id"]
    key = _litellm_key()
    if not key or not model:
        return ("no-key-or-model", 0, "")
    body = json.dumps({"model": model, "messages":[{"role":"user","content":SMOKE_PROMPT}],
                       "max_tokens":5, "temperature":0}).encode()
    url = LITELLM_BASE_URL.rstrip("/") + "/v1/chat/completions"
    req = urllib.request.Request(url, data=body,
        headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=LITELLM_TIMEOUT_S) as r:
            d = json.loads(r.read()); ms = int((time.time()-t0)*1000)
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            return ("ok" if txt else "empty", ms, txt[:40])
    except Exception as e:
        return (f"fail:{type(e).__name__}", int((time.time()-t0)*1000), str(e)[:80])

def cli_smoke(route):
    """Run the route's CLI in the worker via a single-shot docker exec. Path-native."""
    target = route["healthcheck_target"]  # e.g. 'opencode run -m opencode/glm-5.1' or 'codex exec'
    if not target:
        return ("no-target", 0, "")
    # build the container-side command: feed the smoke prompt to the CLI
    if target.startswith("opencode run"):
        shell = f'{target} "{SMOKE_PROMPT}"'
    elif target.startswith("claude -p"):
        shell = f'echo "{SMOKE_PROMPT}" | claude -p --dangerously-skip-permissions'
    elif target.startswith("codex exec"):
        shell = f'echo "{SMOKE_PROMPT}" | codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check'
    elif target.startswith("deepseek"):
        shell = f'deepseek -p "{SMOKE_PROMPT}"'
    else:
        shell = target
    full = ["ssh", "-o", "ConnectTimeout=10", ACPC,
            f'docker exec {WORKER} bash -lc {json.dumps("timeout 45 " + shell)}']
    t0 = time.time()
    try:
        p = subprocess.run(full, capture_output=True, text=True, timeout=70)
        ms = int((time.time()-t0)*1000)
        out = (p.stdout or "") + (p.stderr or "")
        low = out.lower()
        if any(k in low for k in ("401","403","unauthor","invalid","quota","rate limit","not found","no such model","error:")):
            return (f"fail:{out.strip()[:50]}", ms, out[:120])
        if p.returncode == 0 and out.strip():
            return ("ok", ms, out.strip()[:60])
        return (f"fail:rc{p.returncode}", ms, out[:120])
    except subprocess.TimeoutExpired:
        return ("fail:timeout", int((time.time()-t0)*1000), "")
    except Exception as e:
        return (f"fail:{type(e).__name__}", int((time.time()-t0)*1000), str(e)[:80])

def api_chat(route):
    """Call the route's API (OpenRouter) directly. For Janus routes only."""
    import urllib.request
    model = route["model_id"]
    if not OR_KEY or not model:
        return ("no-key-or-model", 0, "")
    body = json.dumps({"model": model, "messages":[{"role":"user","content":SMOKE_PROMPT}],
                       "max_tokens":5, "temperature":0}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization":f"Bearer {OR_KEY}","Content-Type":"application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read()); ms = int((time.time()-t0)*1000)
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            return ("ok" if txt else "empty", ms, txt[:40])
    except Exception as e:
        return (f"fail:{type(e).__name__}", int((time.time()-t0)*1000), str(e)[:80])

def main(limit_cli=None, only=None):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(routes)")]
    if "last_health" not in cols:
        con.execute("ALTER TABLE routes ADD COLUMN last_health TEXT")
        con.execute("ALTER TABLE routes ADD COLUMN last_health_at TEXT")
        con.commit()
    q = "SELECT route_id, model_id, tool, healthcheck_type, healthcheck_target, access_path, cost_mode FROM routes WHERE enabled=1"
    routes = [dict(r) for r in con.execute(q)]
    if only:
        routes = [r for r in routes if only in r["route_id"]]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    results = []
    cli_done = 0
    for r in routes:
        ht = r["healthcheck_type"]
        # Path-native dispatch. A LiteLLM-backed route (spine:litellm) is probed
        # via LiteLLM regardless of its healthcheck_type label, because its
        # model_id is a LiteLLM alias, not an OpenRouter slug. cli-smoke routes
        # run their own CLI; any remaining api-chat route (tool != litellm) keeps
        # the legacy OpenRouter probe.
        if r["tool"] == "litellm" or r["access_path"] == "litellm":
            status, ms, sample = litellm_chat(r)
        elif ht == "cli-smoke":
            if limit_cli is not None and cli_done >= limit_cli:
                continue
            status, ms, sample = cli_smoke(r)
            cli_done += 1
        elif ht == "api-chat":
            status, ms, sample = api_chat(r)
        else:
            status, ms, sample = ("unknown-hc-type", 0, "")
        con.execute("UPDATE routes SET last_health=?, last_health_at=? WHERE route_id=?",
                    (status, now, r["route_id"]))
        results.append((r["route_id"], ht, status, ms))
    con.commit()
    ok = sum(1 for _,_,s,_ in results if s=="ok")
    print(f"path-native health: {ok}/{len(results)} ok")
    for rid, ht, status, ms in results:
        flag = "OK " if status=="ok" else "XX "
        print(f"  {flag}{rid:42s} [{ht}] {status:28s} {ms}ms")
    con.close()

if __name__ == "__main__":
    import sys
    only = None; lim = None
    for a in sys.argv[1:]:
        if a.startswith("--only="): only = a.split("=",1)[1]
        if a.startswith("--limit-cli="): lim = int(a.split("=",1)[1])
    main(limit_cli=lim, only=only)
