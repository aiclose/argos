# Argos operational notes

Hard-won operational facts for running and deploying Argos. Read before deploying or debugging.

## Topology - WHICH HOST

- **Argos runs on GARAGE (192.168.4.40), port 3020**, systemd unit `argos.service`
  (ExecStart: `uvicorn router:app --host 0.0.0.0 --port 3020`, WorkingDirectory `/home/andy/argos`).
- **UM780:3020 is a DIFFERENT service** - the forge-orchestrator
  (`{"service":"forge-orchestrator"}`). It has NO `/route-v2`, `/route`, `/dispatch-record`.
- Therefore: when testing Argos endpoints over the exec tunnel (which runs on UM780),
  `curl 127.0.0.1:3020` hits forge-orchestrator, NOT Argos. You MUST ssh to garage first:
  `ssh andy@192.168.4.40 'curl 127.0.0.1:3020/route-v2 ...'`. A 404 on a known Argos endpoint
  almost always means you hit the wrong host.

## Deploying code changes to argos.service - PYCACHE / RESTART

- A plain `sudo systemctl restart argos.service` can leave the service running STALE bytecode:
  the running app continued to serve an old route set even after the file on disk was updated.
  Symptom: `/openapi.json` lists a path but with empty methods (`methods: []`), or new endpoints
  return 404 while `import router` in the venv shows them bound correctly.
- FIX / required deploy procedure for argos.service:
  1. Deploy the new file(s) to `/home/andy/argos/`.
  2. `find /home/andy/argos -name __pycache__ -type d -exec rm -rf {} +`  (clear stale .pyc)
  3. `sudo systemctl stop argos.service` ; confirm `ss -tlnp | grep :3020` shows 0 listeners
     (kill any orphan pid if present) ; `sudo systemctl start argos.service`  (full stop/start,
     NOT restart).
  4. Verify: `curl 127.0.0.1:3020/openapi.json` (on garage) shows the expected path count, and the
     changed endpoint actually responds.
- NOTE: the orchestrator.service on UM780 does NOT have this problem - its restart regenerated
  bytecode correctly. dispatch_tail is a cron-invoked script (no long-lived process), so it always
  runs current code. The stale-pyc issue is specific to the long-lived argos uvicorn.

## Running maintenance scripts (backfills etc.)

- Argos modules import `fastapi` (via `router`), so backfill scripts that import `router` to reach
  `ARGOS_DB` MUST be run with the venv python:
  `cd /home/andy/argos && ./venv/bin/python3 <script>.py`  - NOT system python3.

## Shadow mode

- Argos is SHADOW (recommends + logs, does not steer live traffic). `healthz` shows
  `"shadow_mode": true`. All Sprint -1 input-quality units (U1-U4) preserved this. Do not flip it
  without the explicit go-live sprint.
