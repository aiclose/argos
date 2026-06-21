#!/usr/bin/env python3
"""Offline smoke test for U5-001: upgraded weekly LLM-judge.

Runnable: `python3 tests/smoke_u5.py`. FULLY OFFLINE -- the real judge() does N=3
OpenRouter calls, so every case here monkeypatches weekly_eval_sprt.or_chat to
return canned judge responses. NO network is ever touched.

Proves the SCORING MACHINERY (not the model's behaviour):
  * parse_rubric handles BOTH the space-separated string form and the JSON-dict
    form, normalising weights to ~1.0.
  * parse_rubric degenerate input (empty / zero-weights) -> [("correctness",1.0)].
  * judge() with good per-criterion JSON -> correct weighted final.
  * NEGATIVE contract: low correctness sub-score (confident-wrong) -> low final;
    high correctness (correct) -> high final; wrong < correct.
  * malformed (non-JSON) judge output -> tolerant fallback to a number, else 0.0,
    NEVER raises.
  * N=3 aggregation: 3 different sample JSONs -> documented aggregate
    (weighted sum of per-criterion means) + variance reported in judge_detailed.
  * judge() returns a float in [0,1]; judge_detailed returns the documented dict.
  * latency-only / expected-None task -> graceful neutral pass, no crash, no call.

Prints PASS/FAIL per case; ends with "SMOKE U5: ALL PASS" or "SMOKE U5: FAIL".
"""
import os, sys, json

# import module under test (repo root is the parent of this tests/ dir)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import weekly_eval_sprt as W

PASS = True
def check(name, cond):
    global PASS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        PASS = False

def approx(a, b, eps=1e-9):
    return abs(a - b) < eps


# --- or_chat mock plumbing ----------------------------------------------------
# Each mock returns (content_str, usage_dict) just like the real or_chat. We swap
# W.or_chat for the duration of each case so judge_detailed picks it up via the
# module global at call time.
class _Canned:
    """Returns successive canned responses round-robin across the N=3 calls."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
    def __call__(self, model, prompt, max_tokens=600, timeout=90, temperature=0.2):
        r = self.responses[self.calls % len(self.responses)]
        self.calls += 1
        return r, {}

def with_mock(responses, fn):
    orig = W.or_chat
    mock = _Canned(responses)
    W.or_chat = mock
    try:
        return fn(), mock
    finally:
        W.or_chat = orig


print("== U5-001 smoke: upgraded weekly LLM-judge (offline, mocked or_chat) ==")

# === Case 1: parse_rubric -- string form ======================================
r = W.parse_rubric("code-correctness:0.6 docstring:0.15 doctests-present:0.15 style:0.1")
names = [c for c, _ in r]
wsum = sum(w for _, w in r)
check("parse_rubric string: 4 criteria parsed",
      names == ["code-correctness", "docstring", "doctests-present", "style"])
check("parse_rubric string: weights normalised to ~1.0", approx(wsum, 1.0))
check("parse_rubric string: code-correctness weight 0.6 preserved after norm",
      approx(dict(r)["code-correctness"], 0.6))

# === Case 2: parse_rubric -- JSON-dict form ===================================
r2 = W.parse_rubric({"code-correctness": 0.6, "style": 0.15, "edge-cases": 0.25})
check("parse_rubric dict: 3 criteria parsed", len(r2) == 3)
check("parse_rubric dict: weights normalised to ~1.0", approx(sum(w for _, w in r2), 1.0))
# also tolerate a JSON object handed in as a STRING
r2s = W.parse_rubric('{"a": 1, "b": 3}')
check("parse_rubric dict-as-string: normalises (a=0.25, b=0.75)",
      approx(dict(r2s)["a"], 0.25) and approx(dict(r2s)["b"], 0.75))

# === Case 3: parse_rubric -- weights that don't sum to 1.0 get normalised ======
r3 = W.parse_rubric("a:2 b:2")  # sums to 4, must normalise to 0.5/0.5
check("parse_rubric: non-1.0 sums normalised to 0.5/0.5",
      approx(dict(r3)["a"], 0.5) and approx(dict(r3)["b"], 0.5))

# === Case 4: parse_rubric -- degenerate input -> fallback =====================
check("parse_rubric empty string -> [('correctness',1.0)]",
      W.parse_rubric("") == [("correctness", 1.0)])
check("parse_rubric None -> [('correctness',1.0)]",
      W.parse_rubric(None) == [("correctness", 1.0)])
check("parse_rubric zero-weights -> [('correctness',1.0)]",
      W.parse_rubric("a:0 b:0") == [("correctness", 1.0)])
check("parse_rubric junk (no colons) -> [('correctness',1.0)]",
      W.parse_rubric("garbage with no weights") == [("correctness", 1.0)])

# === Case 5: judge() with good per-criterion JSON -> correct weighted final ====
RUBRIC = "code-correctness:0.6 docstring:0.15 doctests-present:0.15 style:0.1"
task = {"prompt": "Write a function", "rubric": RUBRIC, "expected": "def f(): ..."}
good = json.dumps({"code-correctness": 0.9, "docstring": 0.8,
                   "doctests-present": 0.7, "style": 1.0})
# expected weighted = .9*.6 + .8*.15 + .7*.15 + 1.0*.1 = .54+.12+.105+.10 = .865
(score, _m) = with_mock([good], lambda: W.judge(task, "some answer"))
check("judge good-JSON: weighted final == 0.865", approx(score, 0.865, 1e-6))
check("judge returns a float in [0,1]", isinstance(score, float) and 0.0 <= score <= 1.0)

# === Case 6: NEGATIVE contract -- confident-wrong < correct ===================
# Same rubric. "wrong" answer gets LOW correctness; "correct" gets HIGH. The
# machinery must propagate a low correctness sub-score into a low FINAL score.
wrong_json = json.dumps({"code-correctness": 0.05, "docstring": 0.6,
                         "doctests-present": 0.5, "style": 0.9})
right_json = json.dumps({"code-correctness": 0.95, "docstring": 0.6,
                         "doctests-present": 0.5, "style": 0.9})
(s_wrong, _) = with_mock([wrong_json], lambda: W.judge(task, "confidently wrong"))
(s_right, _) = with_mock([right_json], lambda: W.judge(task, "correct"))
check("negative: confident-wrong scores LOWER than correct", s_wrong < s_right)
check("negative: low correctness (weight 0.6) drags final below pass",
      s_wrong < W.PASS_THRESHOLD)
check("negative: correct answer clears pass threshold", s_right >= W.PASS_THRESHOLD)

# === Case 7: malformed judge output -> tolerant fallback, NO exception ========
# 7a: not JSON at all but contains a number -> falls back to that number.
raised = False
try:
    (s_num, _) = with_mock(["The score is 0.42 out of 1.0"],
                           lambda: W.judge(task, "ans"))
except Exception:
    raised = True; s_num = None
check("malformed-with-number: no exception", not raised)
check("malformed-with-number: tolerant fallback to 0.42", approx(s_num, 0.42, 1e-6))
# 7b: pure garbage, no number -> 0.0, still no raise.
raised2 = False
try:
    (s_junk, _) = with_mock(["complete nonsense no digits here"],
                            lambda: W.judge(task, "ans"))
except Exception:
    raised2 = True; s_junk = None
check("malformed-no-number: no exception", not raised2)
check("malformed-no-number: falls back to 0.0", approx(s_junk, 0.0))
# 7c: judge JSON wrapped in markdown fences + prose -> tolerant JSON extraction.
fenced = "Here is my grade:\n```json\n" + good + "\n```\nDone."
(s_fenced, _) = with_mock([fenced], lambda: W.judge(task, "ans"))
check("tolerant: JSON extracted from fenced/prose-wrapped output == 0.865",
      approx(s_fenced, 0.865, 1e-6))

# === Case 8: N=3 aggregation -> documented aggregate + variance ===============
# Three DIFFERENT sample JSONs. Documented aggregate = weighted sum of per-criterion
# MEANS across the 3 samples. Compute the expectation independently here.
s1 = {"code-correctness": 0.6, "docstring": 0.5, "doctests-present": 0.4, "style": 0.3}
s2 = {"code-correctness": 0.9, "docstring": 0.8, "doctests-present": 0.7, "style": 0.6}
s3 = {"code-correctness": 0.3, "docstring": 0.2, "doctests-present": 0.1, "style": 0.0}
samples = [json.dumps(s1), json.dumps(s2), json.dumps(s3)]
weights = dict(W.parse_rubric(RUBRIC))
per_mean = {k: (s1[k] + s2[k] + s3[k]) / 3 for k in s1}
expect_final = sum(per_mean[k] * weights[k] for k in per_mean)
# per-sample weighted finals (for the variance check)
finals = [sum(s[k] * weights[k] for k in s) for s in (s1, s2, s3)]
mean_f = sum(finals) / 3
expect_var = sum((f - mean_f) ** 2 for f in finals) / 3

(detail, mock3) = with_mock(samples, lambda: W.judge_detailed(task, "ans"))
check("N=3: or_chat called exactly JUDGE_SAMPLES (3) times", mock3.calls == 3)
check("N=3: n_samples == 3", detail["n_samples"] == 3)
check("N=3: final == weighted sum of per-criterion means",
      approx(detail["score"], expect_final, 1e-9))
check("N=3: variance reported and matches per-sample-finals variance",
      approx(detail["variance"], expect_var, 1e-9))
check("N=3: variance > 0 for disagreeing samples", detail["variance"] > 0)
check("N=3: per_criterion means reported for every criterion",
      set(detail["per_criterion"].keys()) == set(weights.keys()))
check("N=3: samples list holds the 3 per-sample finals",
      len(detail["samples"]) == 3 and all(approx(a, b) for a, b in zip(sorted(detail["samples"]), sorted(finals))))

# === Case 9: judge_detailed dict shape ========================================
(d2, _) = with_mock([good], lambda: W.judge_detailed(task, "ans"))
check("judge_detailed: has documented keys",
      {"score", "per_criterion", "samples", "variance", "n_samples"} <= set(d2.keys()))
check("judge_detailed: score is float in [0,1]",
      isinstance(d2["score"], float) and 0.0 <= d2["score"] <= 1.0)

# === Case 10: latency-only / expected-None -> graceful, no network call =======
lat_task = {"prompt": "ping", "rubric": "latency-only:1.0", "expected": None,
            "id": "live-traffic"}
# Use a mock that RAISES if called -- proves latency-only short-circuits before any call.
def _boom(*a, **k):
    raise AssertionError("or_chat must NOT be called for latency-only tasks")
orig = W.or_chat
W.or_chat = _boom
try:
    crashed = False
    try:
        ld = W.judge_detailed(lat_task, "anything")
        lf = W.judge(lat_task, "anything")
    except Exception:
        crashed = True
finally:
    W.or_chat = orig
check("latency-only: no crash", not crashed)
check("latency-only: judge() returns sensible neutral pass score",
      approx(lf, W.LATENCY_NEUTRAL_SCORE) and lf >= W.PASS_THRESHOLD)
check("latency-only: judge_detailed n_samples == 0 (no model call)",
      ld["n_samples"] == 0)

# === Case 11: expected-None on a NON-latency rubric also short-circuits ========
none_task = {"prompt": "x", "rubric": "code-correctness:1.0", "expected": None}
W.or_chat = _boom
try:
    ne = W.judge(none_task, "ans")
finally:
    W.or_chat = orig
check("expected-None: neutral pass, no model call (no raise)",
      approx(ne, W.LATENCY_NEUTRAL_SCORE))

# === Case 12: all judge calls fail -> 0.0, never raises =======================
def _fail(*a, **k):
    raise RuntimeError("simulated OpenRouter outage")
W.or_chat = _fail
try:
    rerr = False
    try:
        sf = W.judge(task, "ans")
        df = W.judge_detailed(task, "ans")
    except Exception:
        rerr = True
finally:
    W.or_chat = orig
check("all-calls-fail: judge() does not raise", not rerr)
check("all-calls-fail: judge() returns 0.0", approx(sf, 0.0))
check("all-calls-fail: judge_detailed n_samples == 0", df["n_samples"] == 0)

print()
print("SMOKE U5: ALL PASS" if PASS else "SMOKE U5: FAIL")
sys.exit(0 if PASS else 1)
