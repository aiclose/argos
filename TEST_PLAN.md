# Argos Test Plan

Test plan for the Argos allocation engine. Covers route health, the cost model,
route selection, outcome labelling, and the champion-challenger loop. Tests are
grouped by subsystem with the rationale, method, and pass criteria for each.
Principle: tests must be cheap and debuggable; anything touching paid APIs is
gated to free/cheap routes and small sample sizes.

---

## 1. Route health checks

Rationale: before trusting a route, confirm it actually reaches a working model
and returns sane output. This is the most urgent gap: the 42 Forge routes (Zen
and Go especially) have not been verified end to end, and Pharos/OpenRouter
routes are not yet seeded.

### 1.1 Reachability per route

- Method: for each enabled route, send a trivial fixed prompt ("Reply with the
  single word OK") through that route's actual access path (codex CLI, opencode
  --model for Go and Zen, deepseek API, openrouter for Pharos) with a short
  timeout and tiny max-tokens.
- Pass: non-empty response within timeout, no auth/quota error.
- Record: per route, last_check timestamp, ok/fail, latency, error string.
- Frequency: weekly, plus on-demand before a route is first used for real work.
- Budget guard: free and sunk routes first; per-token routes use the smallest
  possible token budget; the whole sweep should cost cents, not dollars.

### 1.2 Classification sanity

- Method: confirm each route's cost_mode and model_id are correct: sunk and
  flat_rate_capped carry no cash cost; per_token routes join model_prices.
- Pass: 39/42 routes join model_prices (known); the 3 that do not are sunk
  (codex, claude-code) plus any deliberate exception, and are flagged not broken.

### 1.3 Capped-lane quota wiring

- Method: once real Go quota limits are entered into route_capacity, verify
  limit_units is populated and resets_at advances correctly across the
  rolling_5h / weekly / monthly windows.
- Pass: capacity_cost stops returning the nominal floor and begins to vary with
  remaining quota.

---

## 2. Cost model tests

### 2.1 effective_cost ranking

- Method: run cost.py over all enabled routes for a representative task; inspect
  the ranking.
- Pass (current state, limits unknown): all sunk/capped lanes at the nominal
  floor (~$0.0005); per_token lanes ordered by real token cost (DeepSeek
  cheapest paid, frontier models most expensive). This was verified on
  2026-05-30.
- Pass (limits known): capped lanes diverge from the floor as used_units rises
  toward (limit - reserve); a near-exhausted lane prices above a cheap paid
  fallback.

### 2.2 Capacity-curve shape

- Method: unit-test phi(R) across the three regimes with synthetic
  limit/used/reserve values.
- Pass: phi = 0 above reserve+buffer; quadratic ramp from 0 to eta across the
  buffer band; eta plus steep quadratic below reserve. Monotonic
  non-decreasing as R falls.

### 2.3 Time decay

- Method: hold quota fixed, vary tau (time to reset) from 1 to 0.
- Pass: capacity_cost decreases as tau falls (use-it-or-lose-it), but only after
  the reserve penalty is applied; never spends aggressively at window start
  purely because a reset exists later.

### 2.4 Pacing multiplier

- Method: simulate a sequence of dispatches consuming quota faster, then slower,
  than the straight-line target; observe lambda.
- Pass: lambda rises under overspend, falls under underspend, never negative.

---

## 3. Route selection tests

### 3.1 Cheapest-clearing-floor selection

- Method: call /route-v2 for varied task classes and stakes; inspect the plan.
- Pass: returns a route, an effective cost, the task-class floor, predicted
  success, cleared-floor flag, a fallback chain, and a rationale. Selected route
  is the cheapest among those clearing the floor.
- Current-state note: with no per-route signal, all routes tie at floor and the
  cheapest (codex) wins for every class. This is correct given the data and is
  itself the test result, not a defect.

### 3.2 Floor gating

- Method: synthetically set one route's observed accept rate for a class below
  the floor and another above (>= 5 labelled dispatches each).
- Pass: the below-floor route is excluded from the clearing pool; selection
  prefers an above-floor route even at higher cost.

### 3.3 No-route-clears fallback

- Method: a task class where no route's predicted success reaches the floor.
- Pass: falls back to the best-utility route rather than erroring; rationale
  flags the fallback.

### 3.4 Non-regression of /route

- Method: confirm the original /route endpoint still responds after each change.
- Pass: returns a selection; Argos stays active on 3020 in shadow mode.

---

## 4. Outcome labelling tests

### 4.1 Status mapping

- Method: unit-test status_to_label across completed / completed_no_checkpoint /
  ok / errored / failed_error / failed_cost_cap / failed\* / unknown.
- Pass: success statuses give accepted=1, failure statuses accepted=0, unknown
  gives null; quality proxy 0.8/0.2 set only where quality_score is null.

### 4.2 No clobbering

- Method: run the labeler twice; include rows with pre-existing real labels.
- Pass: idempotent; never overwrites an existing accepted value or a real
  quality_score.

### 4.3 Coverage

- Method: count labelled dispatches before and after backfill.
- Pass: labelled count rises to cover all rows with a known status (136/138 as
  of 2026-05-30; the 2 unlabelled have null status).

---

## 5. Champion-challenger (SPRT) tests

### 5.1 SPRT step and boundaries

- Method: unit-test sprt_step and the boundary computation for alpha=0.05,
  beta=0.10.
- Pass: log-LR increases on challenger success, decreases on failure; promote
  boundary ~2.89, reject boundary ~-2.25.

### 5.2 Data-driven p0

- Method: run with a class that has >= 5 labelled incumbent dispatches, and one
  with fewer.
- Pass: the first uses the incumbent's observed accept rate as p0; the second
  falls back to 0.70. p0_source recorded in the verdict.

### 5.3 Promotion guards

- Method: drive a short run (n < 30) to a promote LLR; drive a run where the
  rolling-10 no longer favours the challenger.
- Pass: neither promotes; the verdict records the block reason (n<30, or
  rolling-10 no longer favours challenger).

### 5.4 Dethrone-before-promote

- Method: on a DB copy, promote model A for a class, then promote model B.
- Pass: exactly one active (non-dethroned) champion per class afterward; A is
  dethroned, B is current. Verified on 2026-05-30.

### 5.5 Judge robustness (when budget allows)

- Method: run a small bake-off and inspect judge scores for position/verbosity
  bias.
- Pass: answer-order swaps do not flip the winner; longer answers are not
  systematically scored higher.

---

## 6. Integration and safety

### 6.1 Shadow-mode invariant

- Method: after every change, confirm Argos recommends but does not execute.
- Pass: shadow_mode true on /healthz; no real dispatch is triggered by a routing
  call.

### 6.2 Service health

- Method: restart argos.service; confirm it returns active on 3020 and /healthz
  is green.
- Pass: clean restart under systemd; one process, correct port.

### 6.3 Backups before schema change

- Method: confirm argos.db is backed up before any migration.
- Pass: a timestamped argos.db.bak-\* exists prior to each schema change.

---

## 7. Test execution status (2026-05-30)

Verified this session: effective_cost ranking (2.1); /route-v2 behaviour and
non-regression of /route (3.1, 3.4); labeler coverage and mapping (4.1, 4.3);
SPRT clean run and dethrone-before-promote (5.1, 5.4); shadow-mode invariant and
service health (6.1, 6.2).

Not yet executed (highest priority first): route reachability sweep (1.1) for
the Zen and Go routes; capped-lane quota wiring (1.3, blocked on real Go limits);
floor-gating selection (3.2, blocked on per-route labelled data); judge
robustness (5.5).
