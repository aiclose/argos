"""Argos route-aware selection (predict-then-optimise over ROUTES).

Additive upgrade to the Phase-1 router. The existing /route endpoint picks the
cheapest model within a tier from model_prices. This module instead selects over
the `routes` table (so Go vs Zen vs OpenRouter for the same model are distinct
options), scores each route by effective_cost (the shadow-price module), and
picks the CHEAPEST route whose predicted success clears the task-class quality
floor. Still shadow-mode: it recommends and logs, it does not execute.

Honest about the data: with almost no labelled outcomes yet (quality_score is
mostly NULL), predicted success leans on the task-class floor; this gets sharper
automatically as the predictor calibrates on accumulated accept/reject labels.

Exposed as select_route(task) so router.py can call it from a /route-v2 endpoint.
"""
from __future__ import annotations
import logging
import os
import re
import sqlite3, json
from dataclasses import dataclass, field
from typing import Mapping, Optional

try:
    import yaml
except ModuleNotFoundError:  # keep policy loading dependency-free in minimal envs
    yaml = None

import cost as costmod  # the effective_cost module built alongside this
import route_priors  # benchmark-seeded (stale) warm-start priors
import route_priors_dynamic  # U4: live panel/champion warm-start boost (shadow-only)

DB_PATH = "/home/andy/argos/argos.db"
POLICY_PATH = os.path.join(os.path.dirname(__file__), "argos-policy.yaml")
DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(__file__), "argos-policy.defaults.yaml")
BACKEND_FORGE = "forge"
BACKEND_SPINE = "spine"  # the not-Forge backend: langgraph-spine fronting LiteLLM
# Naming lineage: orchestrator -> Janus -> Pharos -> RETIRED (LGSPINE-003/004).
# "janus" is accepted on input as a deprecated alias only; never emitted.
LEGACY_BACKEND_ALIASES = {"janus": BACKEND_SPINE}
BACKEND_GATE_REASONS = {
    "override",
    "forge_predicate",
    "spine_predicate",
    "task_class",
    "ambiguous",
    "lexical-inferred",
}
SOFT_EXEC_FLAVOURED_VERBS = {"build", "fix", "refactor", "test"}

# CHG-P9-052 dual-gate. The route gate has two predicates (see select_route):
#   clears_accept  = predicted_success >= accept_floor  (task_classes floor, now on
#                    the ACCEPT-RATE scale; recalibrated by the floor migration).
#   clears_quality = (gate_mode == basic) OR benchmark_quality >= q_min  (strict only).
# accept_floor is IDENTICAL in both modes; mode toggles ONLY clears_quality.
GATE_MODES = {"basic", "strict"}
DEFAULT_GATE_MODE = "strict"
GATE_MODE_ENV = "ARGOS_GATE_MODE"
# Canonical bake-off judge for benchmark_quality lookups. Mirrors
# weekly_eval_sprt.JUDGE_MODEL -- a bench_cache score is only comparable within one
# judge (U5), so the quality screen reads exactly this judge's scores.
CANONICAL_JUDGE = "google/gemini-2.5-flash"

logger = logging.getLogger(__name__)


@dataclass
class RouteTask:
    tag: str = ""
    task_class: Optional[str] = None
    error_sensitivity: Optional[str] = None
    estimated_input_tokens: int = 4000
    estimated_output_tokens: int = 1500
    explicit_backend_override: Optional[str] = None
    predicates: Optional[Mapping[str, bool]] = None
    prompt: str = ""
    required_capabilities: tuple[str, ...] = ()


@dataclass
class RoutePlan:
    selected_route: Optional[str]
    selected_model: Optional[str]
    cost_mode: Optional[str]
    effective_cost: float
    quality_floor: float
    predicted_success: float
    cleared_floor: bool
    fallback_chain: list = field(default_factory=list)
    candidate_count: int = 0
    rationale: str = ""
    backend: Optional[str] = None
    backend_reason: Optional[str] = None
    scorer_score: float = 0.0
    error: Optional[dict] = None

    @property
    def gated_backend(self) -> Optional[str]:
        return self.backend

    @property
    def gate_reason(self) -> Optional[str]:
        return self.backend_reason


@dataclass(frozen=True)
class BackendGateDecision:
    backend: str
    reason: str


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _parse_policy_scalar(value: str):
    value = value.strip()
    if not value:
        return {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_policy_scalar(part) for part in inner.split(",")]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value.strip("\"'")


def _simple_policy_load(text: str) -> dict:
    """Small YAML subset parser for argos-policy.yaml when PyYAML is absent."""
    root = {}
    stack = [(-1, root)]
    pending_key = None

    for raw in text.splitlines():
        line = _strip_yaml_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        item = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if item.startswith("- "):
            if not isinstance(parent, list):
                if pending_key is None:
                    raise ValueError("list item without parent key")
                parent_map = stack[-2][1]
                parent_map[pending_key] = []
                parent = parent_map[pending_key]
                stack[-1] = (stack[-1][0], parent)
            parent.append(_parse_policy_scalar(item[2:]))
            continue

        key, sep, value = item.partition(":")
        if not sep:
            raise ValueError(f"invalid policy line: {raw!r}")
        key = key.strip()
        parsed = _parse_policy_scalar(value)
        parent[key] = parsed
        pending_key = key
        if isinstance(parsed, dict):
            stack.append((indent, parsed))

    return root


def _read_policy(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        if yaml is not None:
            policy = yaml.safe_load(f) or {}
        else:
            policy = _simple_policy_load(f.read())
    return policy


def _require_mapping(policy: Mapping, key: str) -> Mapping:
    value = policy.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"policy.{key} must be a mapping")
    return value


def _require_string_list(mapping: Mapping, key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"policy.{key} must be a list of strings")
    return value


def _validate_policy(policy: dict, path: str) -> None:
    if not isinstance(policy, dict):
        raise ValueError(f"policy must be a mapping: {path}")
    if policy.get("version") != 1:
        raise ValueError("policy.version must be 1")

    gate = _require_mapping(policy, "backend_gate")
    _normalise_backend(gate.get("ambiguous_default"))
    _require_string_list(gate, "forge_predicates")
    _require_string_list(gate, "spine_predicates_all")
    _require_string_list(gate, "lexical_forge_verbs")
    _require_string_list(gate, "lexical_spine_verbs")

    task_map = _require_mapping(policy, "task_class_backend")
    for task_class, backend in task_map.items():
        if not isinstance(task_class, str):
            raise ValueError("policy.task_class_backend keys must be strings")
        _normalise_backend(backend)

    scoring = _require_mapping(policy, "scoring")
    for weight_key in ("forge_weights", "spine_weights"):
        weights = scoring.get(weight_key)
        if not isinstance(weights, Mapping):
            raise ValueError(f"policy.scoring.{weight_key} must be a mapping")
        for component in ("health", "quota", "quality", "cost", "latency", "path_preference"):
            if component not in weights:
                raise ValueError(f"policy.scoring.{weight_key}.{component} is required")
            try:
                float(weights[component])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"policy.scoring.{weight_key}.{component} must be numeric") from exc

    path_bonus = scoring.get("path_preference_bonus")
    if not isinstance(path_bonus, Mapping):
        raise ValueError("policy.scoring.path_preference_bonus must be a mapping")
    for mode, bonus in path_bonus.items():
        if not isinstance(mode, str):
            raise ValueError("policy.scoring.path_preference_bonus keys must be strings")
        try:
            float(bonus)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"policy.scoring.path_preference_bonus.{mode} must be numeric") from exc

    quality_floors = _require_mapping(policy, "quality_floors")
    for sensitivity in ("low", "medium", "high", "critical"):
        if sensitivity not in quality_floors:
            raise ValueError(f"policy.quality_floors.{sensitivity} is required")
        try:
            float(quality_floors[sensitivity])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"policy.quality_floors.{sensitivity} must be numeric") from exc

    # CHG-P9-052 dual-gate config. Both keys are OPTIONAL (gate_mode defaults to
    # strict, q_min defaults to no screen) so policies predating this sprint still
    # load; when present they must be well-formed.
    gate_mode = policy.get("gate_mode")
    if gate_mode is not None and str(gate_mode).strip().lower() not in GATE_MODES:
        raise ValueError("policy.gate_mode must be 'basic' or 'strict'")
    q_min = policy.get("q_min")
    if q_min is not None:
        if not isinstance(q_min, Mapping):
            raise ValueError("policy.q_min must be a mapping")
        for key, value in q_min.items():
            try:
                float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"policy.q_min.{key} must be numeric") from exc


def _load_policy(path: str = POLICY_PATH) -> dict:
    try:
        policy = _read_policy(path)
        _validate_policy(policy, path)
        return policy
    except Exception as exc:
        if os.path.abspath(path) == os.path.abspath(DEFAULT_POLICY_PATH):
            raise
        logger.warning(
            "policy load fallback: %s malformed (%s); using %s",
            path,
            exc,
            DEFAULT_POLICY_PATH,
        )
        fallback = _read_policy(DEFAULT_POLICY_PATH)
        _validate_policy(fallback, DEFAULT_POLICY_PATH)
        return fallback


def _normalise_backend(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    backend = str(value).strip().lower()
    if backend in LEGACY_BACKEND_ALIASES:
        canonical = LEGACY_BACKEND_ALIASES[backend]
        logger.warning("deprecated backend name %r normalised to %r", backend, canonical)
        return canonical
    if backend in {BACKEND_FORGE, BACKEND_SPINE}:
        return backend
    raise ValueError(f"unsupported backend override: {value!r}")


def _caller_set_predicates(predicates: Optional[Mapping[str, bool]]) -> bool:
    return bool(predicates)


def _first_verb(text: str) -> Optional[str]:
    match = re.search(r"[A-Za-z][A-Za-z_-]*", text or "")
    return match.group(0).lower() if match else None


def _lexical_backend(prompt: str, gate_policy: Mapping) -> tuple[Optional[str], Optional[str]]:
    """Conservative lexical fallback per locked decision #11575.

    Returns Forge only when the first verb is in policy's hard Forge verb list
    and is not a soft execution-flavoured verb. Configured spine verbs resolve
    spine. Unknown or soft execution-flavoured verbs fall back to ambiguous
    spine instead of receiving a lexical-inferred reason.
    """
    verb = _first_verb(prompt)
    forge_verbs = {str(v).lower() for v in gate_policy.get("lexical_forge_verbs", [])}
    spine_verbs = {str(v).lower() for v in gate_policy.get("lexical_spine_verbs", [])}
    if verb in forge_verbs and verb not in SOFT_EXEC_FLAVOURED_VERBS:
        return BACKEND_FORGE, verb
    if verb in spine_verbs:
        return BACKEND_SPINE, verb
    return None, verb


def backend_gate(
    *,
    explicit_backend_override: Optional[str] = None,
    predicates: Optional[Mapping[str, bool]] = None,
    task_class: Optional[str] = None,
    prompt: str = "",
    policy_path: str = POLICY_PATH,
    policy: Optional[Mapping] = None,
) -> BackendGateDecision:
    """Resolve Forge vs spine in strict backend-gate order.

    Order:
    1. explicit override
    2. any hard Forge predicate true
    3. all spine predicates true
    4. task_class_backend hint
    5. conservative lexical fallback only if the caller set no predicates
    6. ambiguous default
    """
    policy = policy or _load_policy(policy_path)
    gate_policy = policy.get("backend_gate", {}) or {}
    predicate_values = dict(predicates or {})

    override = _normalise_backend(explicit_backend_override)
    if override:
        return BackendGateDecision(override, "override")

    forge_predicates = gate_policy.get("forge_predicates", []) or []
    if any(bool(predicate_values.get(name)) for name in forge_predicates):
        return BackendGateDecision(BACKEND_FORGE, "forge_predicate")

    spine_predicates = gate_policy.get("spine_predicates_all", []) or []
    if spine_predicates and all(bool(predicate_values.get(name)) for name in spine_predicates):
        return BackendGateDecision(BACKEND_SPINE, "spine_predicate")

    task_map = policy.get("task_class_backend", {}) or {}
    if task_class in task_map:
        return BackendGateDecision(_normalise_backend(task_map[task_class]), "task_class")

    if not _caller_set_predicates(predicates) and prompt:
        backend, verb = _lexical_backend(prompt, gate_policy)
        if backend:
            logger.info("backend_gate lexical-inferred verb=%s backend=%s", verb, backend)
            return BackendGateDecision(backend, "lexical-inferred")

    ambiguous_default = _normalise_backend(gate_policy.get("ambiguous_default", BACKEND_SPINE))
    return BackendGateDecision(ambiguous_default or BACKEND_SPINE, "ambiguous")


def _conn():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _quality_floor(con, task: RouteTask) -> float:
    """Floor from task_classes.default_quality_floor; fall back to error_sensitivity."""
    if task.task_class:
        row = con.execute(
            "SELECT default_quality_floor FROM task_classes WHERE class_id=?",
            (task.task_class,)).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    # error-sensitivity fallback
    return {"low": 0.65, "medium": 0.70, "high": 0.85, "critical": 0.90}.get(
        task.error_sensitivity or "medium", 0.70)


def _gate_mode(policy: Mapping) -> str:
    """Resolved gate mode: env ARGOS_GATE_MODE wins, then policy.gate_mode, then
    the default (strict). One switch, env-overridable without editing the policy."""
    env = os.environ.get(GATE_MODE_ENV)
    if env:
        mode = env.strip().lower()
        if mode in GATE_MODES:
            return mode
        logger.warning("ignoring invalid %s=%r (expected basic|strict)", GATE_MODE_ENV, env)
    mode = str(policy.get("gate_mode", DEFAULT_GATE_MODE)).strip().lower()
    return mode if mode in GATE_MODES else DEFAULT_GATE_MODE


def _effective_sensitivity(con, task: RouteTask) -> str:
    """Error-sensitivity to screen by: the task's own, else the class default, else
    medium. Used only to pick a q_min row; the accept_floor path is separate."""
    if task.error_sensitivity:
        return str(task.error_sensitivity).lower()
    if task.task_class:
        row = con.execute(
            "SELECT default_error_sensitivity FROM task_classes WHERE class_id=?",
            (task.task_class,)).fetchone()
        if row and row[0]:
            return str(row[0]).lower()
    return "medium"


def _q_min_for(policy: Mapping, task: RouteTask, sensitivity: str) -> float:
    """LOOSE benchmark-quality threshold for this task (strict mode only). Looked up
    by class_id first, then error-sensitivity. 0.0 (no screen) when q_min is absent."""
    q_min = policy.get("q_min", {}) or {}
    if not isinstance(q_min, Mapping):
        return 0.0
    if task.task_class and task.task_class in q_min:
        return float(q_min[task.task_class])
    if sensitivity in q_min:
        return float(q_min[sensitivity])
    return 0.0


def _benchmark_quality(con, model_id: Optional[str], task_class: Optional[str],
                       judge_model: str = CANONICAL_JUDGE) -> Optional[float]:
    """Mean bake-off benchmark SCORE for model_id on task_class from bench_cache,
    using the canonical judge. task_id is '<task_class>/<slug>', so we match the
    class prefix. Returns None when no benchmark exists (cold-start model, or class
    never benched) -- the caller treats None as 'do not block on quality'.

    Best-effort: a missing bench_cache table or any read error returns None rather
    than raising; the quality screen is a sanity gate, not a correctness dependency.
    """
    if not model_id or not task_class:
        return None
    try:
        row = con.execute(
            "SELECT AVG(score) FROM bench_cache "
            "WHERE model_id=? AND task_id LIKE ? AND judge_model=? AND score IS NOT NULL",
            (model_id, f"{task_class}/%", judge_model)).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0] is not None:
        return float(row[0])
    return None


def evaluate_gate(psucc: float, accept_floor: float, gate_mode: str,
                  benchmark_quality: Optional[float], q_min: float) -> dict:
    """Pure two-predicate gate. The ONLY place clears is decided.

    - clears_accept  = psucc >= accept_floor. accept_floor is the (recalibrated)
      task_classes floor, read on the ACCEPT-RATE scale -- NEVER a quality-score.
      Identical in both modes.
    - clears_quality = True in basic mode; in strict mode it is True when there is
      NO benchmark (benchmark_quality is None -> don't block cold-start models) OR
      the benchmark clears the LOOSE q_min screen.
    - clears = clears_accept AND clears_quality.

    No cost input by construction -- the gate is decoupled from the cost selector.
    """
    clears_accept = psucc >= accept_floor
    if gate_mode == "basic":
        clears_quality = True
    else:
        clears_quality = (benchmark_quality is None) or (benchmark_quality >= q_min)
    return {
        "clears_accept": clears_accept,
        "clears_quality": clears_quality,
        "clears": clears_accept and clears_quality,
    }


def _route_columns(con) -> set[str]:
    return {row["name"] for row in con.execute("PRAGMA table_info(routes)")}


def _predicted_success(con, route, task: RouteTask, floor: float) -> tuple[float, Optional[str]]:
    """Predict route success, in priority order:
    1. Observed accept rate for this route+task_class if >= MIN_OBS labels (real data wins).
    2. Otherwise a benchmark-seeded warm-start prior (route_priors), so routes
       differentiate by known model capability instead of all tying at the floor.
    3. The floor only as a last resort if no prior is available.

    As real route-spread outcomes accumulate, (1) overrides the seed. This is the
    research's warm-start: benchmark priors now, learned rates later.

    U4-001: on the COLD-START tiers (2/3 only) we additionally apply a bounded,
    boost-only nudge from live evidence (panel_decisions / champions) via
    route_priors_dynamic. Tier-1 observed data is authoritative and is returned
    UNCHANGED -- we never boost over real labels. Still shadow: this only sharpens
    the predicted-success prior, it does not touch gates or steer traffic.

    Returns (predicted_success, prior_note). prior_note is None for tier-1 and for
    un-boosted cold starts; otherwise it is a short string describing the boost,
    threaded into the route rationale for transparency.
    """
    MIN_OBS = 5
    mid = route["model_id"]
    if mid and task.task_class:
        row = con.execute(
            "SELECT COUNT(*) n, AVG(CASE WHEN accepted THEN 1.0 ELSE 0.0 END) rate "
            "FROM dispatches WHERE model_used=? AND task_class=? AND accepted IS NOT NULL",
            (mid, task.task_class)).fetchone()
        if row and row["n"] and row["n"] >= MIN_OBS and row["rate"] is not None:
            return float(row["rate"]), None  # real data overrides the seed; NO boost
    # warm-start from benchmark prior (tier 2), or floor as last resort (tier 3)
    seed = route_priors.seed_prior(mid, route["tool"], task.error_sensitivity)
    base = seed if seed is not None else floor
    # U4: bounded, boost-only panel/champion nudge on the cold-start guess only.
    # Best-effort -- a malformed panel JSON or missing table must not raise here.
    try:
        boosted, reason = route_priors_dynamic.champion_panel_boost(
            con, route, task.task_class, base)
    except Exception as exc:  # pragma: no cover - defensive; module is best-effort
        logger.debug("route_priors_dynamic boost skipped: %s", exc)
        boosted, reason = base, None
    if reason:
        return boosted, f"quality_prior {base:.2f}->{boosted:.2f} ({reason})"
    return base, None


def _route_is_path_native_healthy(route) -> bool:
    """Locked decision #11573: a route counts as healthy only when validated via
    its OWN access path, never a proxy. cli-smoke was the proxy for path-native
    when spine routes did not exist.

    Extended per ARGOS-CH4 (LiteLLM-verified spine routes): api-chat health now
    means the spine route was probed over its own LiteLLM access path. So a route
    is healthy when its path-native check passed:
      - cli-smoke (Forge CLI lane)  AND last_health == 'ok', OR
      - api-chat  (LiteLLM/spine)   AND last_health == 'ok'.
    NULL last_health is NEVER healthy (route not yet probed).
    """
    ht = route["healthcheck_type"]
    return ht in ("cli-smoke", "api-chat") and route["last_health"] == "ok"


def _route_quota_ok(con, route, route_cols: set[str]) -> bool:
    if "quota_status" in route_cols:
        status = (route["quota_status"] or "ok").strip().lower()
        if status not in {"ok", "available", "healthy", "pass", "unknown"}:
            return False
    try:
        caps = con.execute(
            "SELECT limit_units, used_units FROM route_capacity WHERE route_id=?",
            (route["route_id"],),
        ).fetchall()
    except sqlite3.Error:
        return True
    for cap in caps:
        limit = cap["limit_units"]
        if limit is not None and (cap["used_units"] or 0) >= limit:
            return False
    return True


def _route_capability_ok(route, route_cols: set[str], required: tuple[str, ...]) -> bool:
    if not required:
        return True
    if "capabilities" not in route_cols:
        return False
    raw = route["capabilities"]
    if raw is None:
        return False
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            available = {str(k) for k, v in parsed.items() if v}
        elif isinstance(parsed, list):
            available = {str(v) for v in parsed}
        else:
            available = {str(parsed)}
    except (TypeError, json.JSONDecodeError):
        available = {part.strip() for part in str(raw).split(",") if part.strip()}
    return set(required).issubset(available)


def _quota_score(con, route) -> float:
    try:
        caps = con.execute(
            "SELECT limit_units, used_units FROM route_capacity WHERE route_id=?",
            (route["route_id"],),
        ).fetchall()
    except sqlite3.Error:
        return 1.0
    ratios = []
    for cap in caps:
        limit = cap["limit_units"]
        if limit is None or limit <= 0:
            continue
        ratios.append(max(0.0, min(1.0, (limit - (cap["used_units"] or 0)) / limit)))
    return min(ratios) if ratios else 1.0


def _latency_value(route, route_cols: set[str]) -> Optional[float]:
    for col in ("latency_ms", "p50_latency_ms", "last_latency_ms"):
        if col in route_cols and route[col] is not None:
            return float(route[col])
    return None


def _normalise_lower_is_better(value: float, values: list[float]) -> float:
    if not values:
        return 1.0
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 1.0
    return 1.0 - ((value - lo) / (hi - lo))


def _policy_weights(policy: Mapping, backend: str) -> Mapping[str, float]:
    scoring = policy.get("scoring", {}) or {}
    key = f"{backend}_weights"
    weights = scoring.get(key, {}) or {}
    return {
        "health": float(weights.get("health", 0.0)),
        "quota": float(weights.get("quota", 0.0)),
        "quality": float(weights.get("quality", 0.0)),
        "cost": float(weights.get("cost", 0.0)),
        "latency": float(weights.get("latency", 0.0)),
        "path_preference": float(weights.get("path_preference", 0.0)),
    }


def _path_preference(policy: Mapping, cost_mode: Optional[str]) -> float:
    scoring = policy.get("scoring", {}) or {}
    bonus = scoring.get("path_preference_bonus", {}) or {}
    return float(bonus.get(cost_mode or "", 0.0))


def select_route(task: RouteTask) -> RoutePlan:
    con = _conn()
    try:
        policy = _load_policy()
        gate = backend_gate(
            explicit_backend_override=task.explicit_backend_override,
            predicates=task.predicates,
            task_class=task.task_class,
            prompt=task.prompt,
            policy=policy,
        )
        floor = _quality_floor(con, task)  # accept_floor: SAME in both gate modes
        gate_mode = _gate_mode(policy)
        sensitivity = _effective_sensitivity(con, task)
        q_min = _q_min_for(policy, task, sensitivity)
        route_cols = _route_columns(con)
        optional = [
            col for col in (
                "healthcheck_type", "last_health", "quota_status", "capabilities",
                "latency_ms", "p50_latency_ms", "last_latency_ms",
            )
            if col in route_cols
        ]
        required_cols = ["route_id", "backend", "tool", "access_path", "model_id", "cost_mode", "enabled"]
        select_cols = required_cols + [col for col in optional if col not in required_cols]
        missing_health = {"healthcheck_type", "last_health"} - route_cols
        if missing_health:
            detail = f"routes table missing health columns: {sorted(missing_health)}"
            return RoutePlan(None, None, None, 0.0, floor, 0.0, False, [], 0,
                             detail, gate.backend, gate.reason, 0.0,
                             {"code": "no_healthy_route", "message": detail})
        routes = con.execute(
            f"SELECT {', '.join(select_cols)} FROM routes WHERE enabled=1").fetchall()
        ctask = costmod.Task(est_input_tokens=task.estimated_input_tokens,
                             est_output_tokens=task.estimated_output_tokens,
                             task_class=task.task_class)
        candidates = []
        excluded = {"backend": 0, "health": 0, "quota": 0, "capability": 0}
        for r in routes:
            if r["backend"] != gate.backend:
                excluded["backend"] += 1
                continue
            if not _route_is_path_native_healthy(r):
                excluded["health"] += 1
                continue
            if not _route_quota_ok(con, r, route_cols):
                excluded["quota"] += 1
                continue
            if not _route_capability_ok(r, route_cols, tuple(task.required_capabilities)):
                excluded["capability"] += 1
                continue
            eff = costmod.effective_cost(r["route_id"], ctask, con)
            psucc, prior_note = _predicted_success(con, r, task, floor)
            # Dual-gate: accept predicate (accept_floor, both modes) AND quality
            # predicate (strict only). Decided here, BEFORE cost scoring -- the
            # eligible set is independent of cost.
            bq = _benchmark_quality(con, r["model_id"], task.task_class) if gate_mode != "basic" else None
            gate_eval = evaluate_gate(psucc, floor, gate_mode, bq, q_min)
            candidates.append({
                "route_id": r["route_id"], "model_id": r["model_id"],
                "cost_mode": r["cost_mode"], "eff": eff,
                "psucc": psucc, "prior_note": prior_note, "clears": gate_eval["clears"],
                "clears_accept": gate_eval["clears_accept"], "clears_quality": gate_eval["clears_quality"],
                "benchmark_quality": bq,
                "quota_score": _quota_score(con, r),
                "path_preference": _path_preference(policy, r["cost_mode"]),
                "latency": _latency_value(r, route_cols),
            })

        if not candidates:
            if gate.backend == BACKEND_SPINE:
                err = {"code": "no_spine_route_available", "message": "no spine route available"}
                rationale = (
                    f"backend={gate.backend} reason={gate.reason} | "
                    f"health_key=path-native(cli-smoke|api-chat),last_health:ok | "
                    f"excluded={excluded} | no spine route available"
                )
                return RoutePlan(None, None, None, 0.0, floor, 0.0, False, [], 0,
                                 rationale, gate.backend, gate.reason, 0.0, err)
            err = {"code": "no_healthy_route", "message": f"no healthy {gate.backend} route available"}
            rationale = (
                f"backend={gate.backend} reason={gate.reason} | "
                f"health_key=path-native(cli-smoke|api-chat),last_health:ok | excluded={excluded}"
            )
            return RoutePlan(None, None, None, 0.0, floor, 0.0, False, [], 0,
                             rationale, gate.backend, gate.reason, 0.0, err)

        costs = [s["eff"] for s in candidates]
        latencies = [s["latency"] for s in candidates if s["latency"] is not None]
        weights = _policy_weights(policy, gate.backend)
        for s in candidates:
            components = {
                "health": 1.0,
                "quota": s["quota_score"],
                "quality": s["psucc"],
                "cost": _normalise_lower_is_better(s["eff"], costs),
                "latency": _normalise_lower_is_better(s["latency"], latencies) if s["latency"] is not None else 1.0,
                "path_preference": s["path_preference"],
            }
            s["score"] = sum(weights[name] * components[name] for name in weights)
            s["components"] = components

        candidates.sort(key=lambda s: (s["score"], s["psucc"], -s["eff"]), reverse=True)
        best = candidates[0]
        fallbacks = [s["route_id"] for s in candidates[1:5]]

        if not best["clears"]:
            err = {"code": "quality_floor_not_met", "message": "no route cleared quality floor"}
            fail = ("accept" if not best["clears_accept"]
                    else "quality" if not best["clears_quality"] else "both")
            bq = best.get("benchmark_quality")
            rationale = (
                f"backend={gate.backend} reason={gate.reason} | gate_mode={gate_mode} | "
                f"accept_floor={floor:.2f} q_min={q_min:.2f} | "
                f"strategy=within-backend-weighted-argmax | "
                f"health_key=path-native(cli-smoke|api-chat),last_health:ok | "
                f"candidates={len(candidates)} excluded={excluded} | "
                f"best={best['route_id']} score={best['score']:.3f} psucc={best['psucc']:.2f} "
                f"bench_q={bq if bq is None else round(bq, 2)} floor_fail={fail}"
            )
            return RoutePlan(None, None, None, round(best["eff"], 6), floor,
                             round(best["psucc"], 3), False, fallbacks,
                             len(candidates), rationale, gate.backend, gate.reason,
                             round(best["score"], 6), err)

        rationale = (
            f"backend={gate.backend} reason={gate.reason} | task_class={task.task_class} | "
            f"gate_mode={gate_mode} accept_floor={floor:.2f} q_min={q_min:.2f} | "
            f"strategy=within-backend-weighted-argmax | "
            f"weights={dict(weights)} | health_key=path-native(cli-smoke|api-chat),last_health:ok | "
            f"candidates={len(candidates)} excluded={excluded} | "
            f"picked {best['route_id']} score={best['score']:.3f} "
            f"eff=${best['eff']:.6f} psucc={best['psucc']:.2f}"
        )
        if best.get("prior_note"):  # U4: surface panel/champion warm-start influence
            rationale += f" | {best['prior_note']}"
        return RoutePlan(
            selected_route=best["route_id"], selected_model=best["model_id"],
            cost_mode=best["cost_mode"], effective_cost=round(best["eff"], 6),
            quality_floor=floor, predicted_success=round(best["psucc"], 3),
            cleared_floor=True, fallback_chain=fallbacks,
            candidate_count=len(candidates), rationale=rationale,
            backend=gate.backend, backend_reason=gate.reason,
            scorer_score=round(best["score"], 6),
        )
    finally:
        con.close()


def demo():
    """Show the plan for a few representative task types."""
    cases = [
        RouteTask(tag="t1", task_class="documentation", error_sensitivity="low"),
        RouteTask(tag="t2", task_class="code_generation", error_sensitivity="high"),
        RouteTask(tag="t3", task_class="test_unit", error_sensitivity="low"),
        RouteTask(tag="t4", task_class="code_algorithmic", error_sensitivity="high",
                  estimated_input_tokens=12000, estimated_output_tokens=4000),
    ]
    for c in cases:
        p = select_route(c)
        print(f"\n[{c.task_class} / {c.error_sensitivity}]")
        print(f"  -> {p.selected_route}  (${p.effective_cost}, {p.cost_mode})")
        print(f"     floor={p.quality_floor} psucc={p.predicted_success} cleared={p.cleared_floor}")
        print(f"     fallbacks: {p.fallback_chain[:3]}")


if __name__ == "__main__":
    demo()
