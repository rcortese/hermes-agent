"""Fork-local, config-authoritative routing for delegated task categories."""

from copy import deepcopy
from dataclasses import dataclass

CATEGORY_LIMITS = {"simples": 40, "analitica": 80, "complexa": 120}
DEFAULT_CATEGORY = "analitica"
DEFAULT_PURPOSE = "general"
PURPOSES = frozenset({DEFAULT_PURPOSE, "development"})
_TASK_FIELDS = {"goal", "context", "role", "category", "purpose", "output_schema", "images", "group"}
_CONFIG_FIELDS = {"model", "provider", "reasoning_effort", "max_iterations", "allowed_models",
                  "allowed_routes", "fallback_providers"}


@dataclass(frozen=True)
class CategoryRoute:
    category: str
    model: str
    provider: str
    reasoning_effort: str
    max_iterations: int
    purpose: str = DEFAULT_PURPOSE
    fallback_providers: tuple[tuple[str, str], ...] = ()

    def credentials_cfg(self):
        """Only trusted routing identifiers reach the native credential resolver."""
        return {"model": self.model, "provider": self.provider,
                "reasoning_effort": self.reasoning_effort,
                "fallback_providers": [{"provider": provider, "model": model}
                                       for provider, model in self.fallback_providers]}

    def receipt(self):
        return {
            "category": self.category,
            "purpose": self.purpose,
            "configured_max_iterations": self.max_iterations,
            "reasoning_effort": self.reasoning_effort,
            "configured_provider": self.provider,
            "configured_model": self.model,
            "configured_chain": [{"provider": self.provider, "model": self.model},
                                 *self.credentials_cfg()["fallback_providers"]],
            "fallback_transitions": [],
        }


def _exact_identifier(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def _route_pairs(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of exact provider/model pairs.")
    pairs = []
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {"provider", "model"}
                or not all(_exact_identifier(entry[key]) for key in ("provider", "model"))):
            raise ValueError(f"{label} accepts only exact provider/model pairs, without extra fields.")
        pair = (entry["provider"], entry["model"])
        if pair in pairs:
            raise ValueError(f"{label} contains a duplicate route.")
        pairs.append(pair)
    return tuple(pairs)


def _validate_route(row, category, purpose):
    label = f"Category {category}/{purpose}"
    if not isinstance(row, dict) or set(row) - _CONFIG_FIELDS:
        raise ValueError(f"Invalid configuration fields for {label}.")
    model, provider = row.get("model"), row.get("provider")
    for name, value in (("model", model), ("provider", provider)):
        if not _exact_identifier(value):
            raise ValueError(f"{label} requires an exact nonempty {name}.")
    allowed = row.get("allowed_models")
    if (not isinstance(allowed, list) or not allowed
            or any(not _exact_identifier(item) for item in allowed) or model not in allowed):
        raise ValueError(f"{label} model must match allowed_models exactly.")
    effort = row.get("reasoning_effort")
    if not isinstance(effort, str) or effort not in ("low", "medium", "high"):
        raise ValueError(f"{label} requires valid reasoning_effort.")
    fallback = _route_pairs(row.get("fallback_providers"), f"{label} fallback_providers")
    primary = (provider, model)
    if primary in fallback:
        raise ValueError(f"{label} fallback_providers contains a primary self-loop.")
    if fallback or "allowed_routes" in row:
        allowed_routes = _route_pairs(row.get("allowed_routes"), f"{label} allowed_routes")
        if any(pair not in allowed_routes for pair in (primary, *fallback)):
            raise ValueError(f"{label} primary and alternatives must match allowed_routes exactly.")
    default_limit = CATEGORY_LIMITS[category]
    limit = row.get("max_iterations", default_limit)
    if type(limit) is not int or limit <= 0 or limit > default_limit:
        raise ValueError(f"{label} max_iterations must be 1..{default_limit}.")
    assert isinstance(model, str) and isinstance(provider, str)
    return CategoryRoute(category, model, provider, effort, limit, purpose, fallback)


def validate_category_routes(cfg, tasks, *, max_iterations=None, task_images=None):
    """Validate the entire owner table and batch before credentials or spawns.

    Purpose selects a configured sub-row, never a caller-supplied model. A
    development row is complete and cannot inherit general or parent policy.
    Identifiers are matched exactly, never stripped or normalized.
    """
    for index, task in enumerate(tasks):
        purpose = task.get("purpose", DEFAULT_PURPOSE)
        if not isinstance(purpose, str) or purpose not in PURPOSES:
            raise ValueError(f"Task {index} has an invalid purpose.")
        images = task_images[index] if task_images is not None else task.get("images")
        if purpose == "development" and images:
            raise ValueError(f"Task {index} development purpose does not accept images.")
    if "categories" not in cfg:
        if any("category" in task or task.get("purpose", DEFAULT_PURPOSE) != DEFAULT_PURPOSE for task in tasks):
            raise ValueError("Task categories/development require delegation.categories in config.yaml.")
        return None
    if max_iterations is not None:
        raise ValueError("Caller max_iterations overrides are forbidden in category mode.")
    table = cfg["categories"]
    if not isinstance(table, dict) or set(table) != set(CATEGORY_LIMITS):
        raise ValueError("delegation.categories must define simples, analitica and complexa exactly.")
    routes = {}
    for category in CATEGORY_LIMITS:
        row = table[category]
        if not isinstance(row, dict):
            raise ValueError(f"Invalid configuration fields for category {category}.")
        routes[(category, DEFAULT_PURPOSE)] = _validate_route(
            {key: value for key, value in row.items() if key != "development"}, category, DEFAULT_PURPOSE)
        if "development" in row:
            routes[(category, "development")] = _validate_route(row["development"], category, "development")
    selected = []
    for index, task in enumerate(tasks):
        if set(task) - _TASK_FIELDS:
            raise ValueError(f"Task {index} contains forbidden overrides or unknown fields.")
        category = task.get("category", DEFAULT_CATEGORY)
        if not isinstance(category, str) or category not in CATEGORY_LIMITS:
            raise ValueError(f"Task {index} has an invalid category.")
        purpose = task.get("purpose", DEFAULT_PURPOSE)
        if (category, purpose) not in routes:
            raise ValueError(f"Task {index} has no configured {category}/{purpose} route.")
        if task.get("context") is not None and not isinstance(task["context"], str):
            raise ValueError(f"Task {index} context must be a string.")
        selected.append(routes[(category, purpose)])
    return selected


def _effective_route(child, receipt):
    # Named custom identity matters: several owners share provider='custom'.
    provider = getattr(child, "requested_provider", None) or getattr(child, "provider", None)
    model = getattr(child, "model", None)
    return {"provider": provider if isinstance(provider, str) else receipt["configured_provider"],
            "model": model if isinstance(model, str) else receipt["configured_model"]}


def category_execution_receipt(child):
    receipt = getattr(child, "_delegation_category_receipt", None)
    if not isinstance(receipt, dict):
        return {}
    receipt = deepcopy(receipt)
    effective = _effective_route(child, receipt)
    receipt.update(effective_provider=effective["provider"], effective_model=effective["model"])
    return receipt


def _observe_category_fallback(child):
    """Observe the bound native switch; eligibility, retries and cooldown stay native.

    The facade forwards to try_activate_fallback(agent, reason=None, reset_at=None).
    Only successful switches carry enum reasons; arbitrary diagnostics never do.
    """
    original = getattr(child, "_try_activate_fallback", None)
    if not callable(original):
        return

    def observed(reason=None, reset_at=None):
        from agent.error_classifier import FailoverReason
        receipt = child._delegation_category_receipt
        before = _effective_route(child, receipt)
        activated = original(reason=reason, reset_at=reset_at)
        if activated:
            receipt["fallback_transitions"].append({
                "from": before, "to": _effective_route(child, receipt),
                "reason": reason.value if isinstance(reason, FailoverReason) else FailoverReason.unknown.value,
            })
        return activated

    child._try_activate_fallback = observed


def install_category_loop_budget(child, route):
    """Share a reported loop-call allowance across conversation re-entries.

    This is NOT a transport request cap: HTTP retries and the loop's finalizer
    retain their existing semantics. Preserve conversation flags verbatim.
    """
    original = child.run_conversation
    used = 0
    reported = 0
    last_result = None
    last_exception = None
    child._delegation_category_receipt = route.receipt()

    def run_with_budget(*args, **kwargs):
        nonlocal used, reported, last_result, last_exception
        remaining = max(0, route.max_iterations - used)
        if not remaining:
            # No retry call, nor a new error that masks the original outcome.
            if last_exception is not None:
                raise last_exception
            assert last_result is not None
            result = deepcopy(last_result)
            result["category_budget_exhausted"] = True
            return result
        child.max_iterations = remaining
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            # An unaccounted partial attempt cannot receive another allowance.
            used = route.max_iterations
            last_exception = exc
            child._delegation_category_accounting_unknown = True
            raise
        last_result = deepcopy(result)
        calls = result.get("api_calls")
        if type(calls) is int and calls >= 0:
            used += calls
            reported += calls
        else:
            # Unknown accounting must not buy a fresh allowance on retry.
            used = route.max_iterations
            child._delegation_category_accounting_unknown = True
        child._delegation_category_api_calls = reported
        return result

    child.run_conversation = run_with_budget
    child._delegation_category_api_calls = 0
    child._delegation_category_receipt = deepcopy(route.receipt())
    _observe_category_fallback(child)
