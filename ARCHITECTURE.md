# Argos: Cost-Aware Multi-Model Allocation Engine

Argos is the allocation brain of the homelab AI stack. It decides which model or
tool handles each task, optimising cost, speed, and proven accuracy
simultaneously. This document describes the system as actually built (2026-05-30):
its architecture, the cost model, the learning algorithms, and the design
choices behind them.

This is reference documentation. The working source of truth is the Obsidian
vault (`_Homelab/canonical/argos-*`); the code lives in Gitea (`admin/argos`,
mirrored to GitHub `aiclose/argos`); the service runs on garage.

---

## 1. Architecture

### 1.1 The role of Argos

Argos is one brain serving two execution backends:

- **Forge** is a dumb CLI execution panel. Its lanes are the coding tools:
  codex CLI (sunk-cost OAuth), claude-code CLI (sunk now, per-token soon),
  DeepSeek (direct API), and OpenCode CLI. OpenCode draws on two distinct
  subscriptions: **Go** (flat-rate ~$10/mo across ~15 open models, usage-capped
  on rolling 5h / weekly / monthly windows, $0 marginal within caps) and **Zen**
  (per-token, stronger model set). Forge contains no intelligence about which
  tool to pick; it is purely the hands.
- **Pharos** is the chat/assistant backend. Its lane is **OpenRouter** (a
  per-token aggregator across many models). Different actuator, different use
  case, but the same allocation brain.

Argos owns route selection, escalation policy, and learning for both. The
backends just execute what Argos chooses.

### 1.2 Routes, not models

The central modelling decision: the unit of allocation is a **route**, not a
model. The same underlying model (say Kimi K2.6) may be reachable through
OpenCode Go (flat-rate capped), OpenCode Zen (per-token), or OpenRouter
(per-token via Pharos), each at a different effective cost. Collapsing these
into one "model" entity hides exactly the cost differences Argos exists to
exploit.

A route is `(backend, tool, access_path, model_id, cost_mode)`, identified by a
string such as `forge:opencode-go:kimi-k2.6` or `pharos:openrouter:deepseek`.
There are currently 42 routes, all on the Forge side. The Zen and Go routes
still need live verification that each reaches a working model; Pharos/OpenRouter
routes are not yet seeded.

### 1.3 Crucible folds in

Crucible, formerly a separate champion-challenger evaluation harness, is now an
evaluation subsystem inside Argos rather than a parallel brain. Its tables
(bake_off_judges, bake_off_rounds, bake_off_decisions, sprt_decisions) live in
argos.db, and the champion-challenger loop is implemented as a script in the
Argos codebase.

### 1.4 Component interaction

- **Argos** reads task specs, the route registry, and historical outcomes;
  writes dispatch plans, predictions, eval requests, and ledger updates.
- **Forge / Pharos** receive a dispatch plan, execute, and return raw output,
  timings, exit codes.
- **Crucible (inside Argos)** runs champion-challenger evaluations and writes
  scores and promotion decisions.
- **The Vault** holds canonical human-authored docs and remains the normative
  source.
- **The Brain (Engram)** supplies memory as features/priors only, never as
  authority over measured route statistics.

### 1.5 Service shape

Argos runs as `argos.service` (systemd) on garage, a FastAPI app on port 3020 in
shadow mode. It is fed by a set of cron jobs: a pricing puller (6h), a drift
detector (daily), a dispatch tail (10 min) that classifies new cost-log entries
into the task taxonomy and logs shadow routing recommendations, a task groomer
(30 min) that decomposes backlog items, a weekly retrain, and daily ROI and
groomer-feedback jobs.

---

## 2. The cost model

### 2.1 Effective cost

Posted price is not the real cost. Sunk-cost and capped lanes priced at literal
zero get overused, burning scarce quota on trivial work and leaving nothing for
important work. Argos therefore prices every route by:

```
effective_cost(route, task) = cash_cost + capacity_cost + congestion_cost
```

- **cash_cost**: per-token routes pay tokens times price plus request overhead,
  read from the model_prices registry. Sunk and flat-rate routes have zero cash
  cost.
- **capacity_cost**: a shadow price for capped/sunk lanes that rises as a usage
  window's remaining quota approaches a reserve held for high-stakes work.
- **congestion_cost**: a latency/queue penalty when a lane is busy. Currently a
  stub returning zero; reserved for a future term derived from recent latency
  and in-flight count.

### 2.2 The capacity-cost curve

The capacity shadow price is piecewise-convex (not linear; linear is too easy to
game near the reserve, and exponential is hard to tune from a few hundred
samples). With remaining quota R, reserve R\*, buffer band b, and a per-window
pacing multiplier lambda:

```
capacity_cost = lambda * phi(R) * tau^gamma

phi(R) = 0                                   if R >= R* + b
phi(R) = eta * ((R*+b-R)/b)^2                if R* < R < R*+b   (quadratic ramp)
phi(R) = eta + kappa * ((R*-R)/max(1,R*))^2  if R <= R*         (steep)
```

Defaults: b = 0.15 \* limit; eta = median cash cost of a decent per-token
fallback for the task class; kappa = 5 \* eta; gamma = 1. The `tau^gamma` term
(tau = fraction of the window remaining) makes quota cheaper as reset
approaches, but only after the reserve penalty applies, so the system does not
spend aggressively early in a window just because a reset exists later.

When a route's real quota limit is unknown (the current state for the Go lanes),
capacity_cost falls back to a small nominal floor (~$0.0005), so capped/sunk
lanes are cheap-but-never-zero. This is why, today, all cheap lanes tie at that
floor and the router differentiates them only on predicted quality.

### 2.3 Pacing multiplier (primal-dual)

The lambda multiplier is the lightweight "proper" technique: primal-dual pacing
with a Lagrange multiplier, the same family used in budget pacing and
bandits-with-knapsacks. After each dispatch:

```
lambda <- max(0, lambda + 0.1 * (u_t - ubar_t) / sqrt(1 + t))
```

where u_t is actual quota consumed and ubar_t is a straight-line target from
`Ubar(t) = (limit - R*) * (1 - tau)`. The multiplier rises if spending is too
fast, falls if too slow. No demand forecasting required.

### 2.4 Reserve shares

Reserve target per task class defaults to manual shares while data is sparse:
50-70% of quota held for high-stakes coding, 20-30% for medium, 0-10% for low.
Soft reservation by default (low-stakes work may cross the boundary if its
utility justifies it); hard buckets only where missing quota for a high-stakes
class would be very costly. Once demand history accumulates, reserve target can
move to the 80th percentile of observed per-class demand plus a cushion.

---

## 3. Algorithms

### 3.1 Route selection: predict-then-optimise

For low-volume single-user data, Argos uses predict-then-optimise rather than a
full contextual bandit. For a task: classify it (task class plus stakes from the
task_classes table), then for each enabled route estimate success probability,
expected latency, and effective cost, then select.

The selection rule (the MVP policy): **pick the minimum-effective-cost route
whose calibrated success probability clears the task-class quality floor; if none
clear it, pick the best-utility route.** After execution, stop on verifier pass,
otherwise escalate only the failed component.

This is exposed as the shadow endpoint `/route-v2`. The original `/route`
(cheapest-model-in-tier) remains for continuity.

### 3.2 Quality prediction

Success prediction is a regularised logistic regression (scikit-learn), chosen
over richer learners because the data is small, interpretability matters, and
routing needs trustworthy probabilities. Features are task-only: task-class
one-hot, error-sensitivity, token-scale, tag-prefix flags, and notes signals (39
dimensions). Current 5-fold CV accuracy is about 0.68 on 136 labelled
dispatches; learned weights are sensible (debugging_simple predicts failure;
testing and code_implementation predict success).

Deliberate limitation: the predictor has **no route feature**. With ~all
historical labels concentrated on one model, adding a route feature would learn
"that one model works" and assign noise to untested routes, presenting "no data"
as a prediction. The predictor stays route-neutral until route-spread outcome
data exists. The maturity path is: regularised logistic, then isotonic/Platt
calibration, then gradient-boosting or hierarchical/partial-pooling only when
data and proven uplift justify it.

### 3.3 Outcome labelling

The predictor needs labels. A binary success label is derived from execution
status already captured on dispatches: completed / completed_no_checkpoint / ok
map to accepted = 1; errored / failed\* map to accepted = 0; unknown stays null.
A coarse quality proxy (0.8 success, 0.2 failure) is set where no finer score
exists, flagged as crude pending real rubric/judge scoring. This took the
labelled set from 2 to 136 dispatches without any new collection, by using
signal that already existed.

### 3.4 The escalation cascade (FrugalGPT pattern)

Router, then cheap route, then verifier, then stop-or-escalate:

- **Hard signals first**: non-zero exit code, timeout, failed tests, syntax
  errors trigger escalation.
- **Cheap quality signals**: rubric sub-scores, judge score, diff size, lint.
- **Combination**: gating logic first (hard fail escalates; hard pass with
  executable verification stops), otherwise a small logistic classifier predicts
  whether escalation would improve net utility.
- **Narrow escalation**: re-run only the failed component (failing tests,
  malformed file, low-confidence section), not the whole task. The groomer
  isolates the weak component after partial failure.

Ground truth for code is tests first (unit, integration, lint, typecheck),
judges second. LLM-as-judge is used pairwise where possible, with answer-order
swaps required for consistency, never the same model family as both contestant
and judge, no reward for length, and judge output treated as noisy evidence.

### 3.5 Champion-challenger promotion (SPRT)

Promotion uses a Wald Sequential Probability Ratio Test with an indifference
zone, so a champion is replaced only on clear, stable evidence rather than noise.

- Hypotheses: p0 = the incumbent's observed accept rate for the class
  (data-driven, falling back to 0.70 only with too little history); p1 = p0 +
  delta, delta = 0.08.
- Error bounds: alpha = 0.05, beta = 0.10. Wald boundaries A = log((1-beta)/alpha)
  to promote, B = log(beta/(1-alpha)) to reject.
- Per trial, both arms run the same task; an LLM judge scores output (pass
  threshold 0.7 gives a binary outcome); the challenger's success stream updates
  the log-likelihood ratio.
- **Promotion guards**: promote only if SPRT accepts the challenger AND there are
  at least 30 paired observations AND the rolling last-10 challenger mean still
  exceeds p0. Hysteresis: the bar to promote a challenger is higher than the bar
  to demote an incumbent. On promotion, the previous champion for that class is
  dethroned before the new one is recorded.
- Promotion is always per task-class, never global.

For continuous rubric scores, the recommended handling is paired differences,
winsorised, binarised as "meaningfully better"; with small samples, stratify by
at most one or two coarse factors.

### 3.6 Exploration (deferred)

Exploration is deliberately not enabled yet. The research-backed gates: enable
only when per-class expected calibration error is below ~0.06, Brier score below
~0.20, and the top routes each have at least 25 observations. When enabled, start
with gated epsilon-greedy (epsilon 0.05-0.10) on cheap routes and low-stakes
tasks only, sampling among routes whose effective cost is below a class ceiling
and whose predicted utility is within ~10% of the best. Graduate to cost-aware
Thompson sampling (sample on net utility U = stakes \* quality - effective_cost,
"explore in dollars, not win-rate") only once posterior uncertainty, not model
misspecification, is the main source of routing regret.

---

## 4. The current bottleneck (honest state)

Argos's machinery is built and correct, but it is not yet smart, and the reason
is data, not code. The router currently selects the same cheapest route (codex)
for every task because: all cheap lanes tie at the nominal capacity floor (real
Go quota limits not yet entered); the predictor is route-neutral; and 106 of 138
historical dispatches went to a single model, so there is no per-route fit
signal. Intelligence will accrue as route-spread labelled data accumulates, which
is precisely what the champion-challenger loop generates. Building more machinery
on top of absent data would be the central anti-pattern for a system at this
scale.

---

## 5. Failure modes and anti-patterns

Watched-for failure modes: treating judge scores as ground truth despite known
biases; collapsing route and model into one entity (hiding cost differences);
optimising average cost instead of cost-to-success; thresholding uncalibrated
probabilities; exhausting sunk-cost lanes then paying peak prices for important
work; promoting challengers globally rather than per task-class; and
over-building a research-grade router for a dataset that does not exist. The
guiding discipline is to start with the simplest defensible method and grow it
only when logged data proves the need.

---

## 6. Prior art

The design borrows from FrugalGPT (cost-aware cascade: router, scorer, stop
judger), RouteLLM (preference-trained routing and cold-start priors), "A Unified
Approach to Routing and Cascading for LLMs" (long-term blueprint), MT-Bench /
Chatbot Arena (judge-bias lessons), and budget-pacing / bandits-with-knapsacks
literature (the capacity shadow price and primal-dual pacing). Evaluation harness
patterns draw on Promptfoo, DeepEval, and Inspect AI.
