"""Fork-local, config-authoritative routing for delegated task categories."""

from copy import deepcopy
from dataclasses import dataclass

CATEGORY_LIMITS = {"simples": 40, "analitica": 80, "complexa": 120}
DEFAULT_CATEGORY = "analitica"
_TASK_FIELDS = {"goal", "context", "role", "category", "output_schema", "images", "group"}
_CONFIG_FIELDS = {"model", "provider", "reasoning_effort", "max_iterations", "allowed_models", "fallback_providers"}


@dataclass(frozen=True)
class CategoryRoute:
    category: str
    model: str
    provider: str
    reasoning_effort: str
    max_iterations: int

    def credentials_cfg(self):
        """Only trusted routing identifiers reach the credential resolver."""
        return {"model": self.model, "provider": self.provider,
                "reasoning_effort": self.reasoning_effort, "fallback_providers": []}

    def receipt(self):
        return {
            "category": self.category,
            "configured_max_iterations": self.max_iterations,
            "reasoning_effort": self.reasoning_effort,
            "configured_provider": self.provider,
            "configured_model": self.model,
        }


def validate_category_routes(cfg, tasks, *, max_iterations=None):
    """Validate the entire table and batch without credentials, writes or spawns.

    Absence is legacy mode; a present but malformed/empty table fails closed.
    Identifiers are matched exactly, never stripped or normalized.
    """
    if "categories" not in cfg:
        if any("category" in task for task in tasks):
            raise ValueError("Task categories require delegation.categories in config.yaml.")
        return None
    if max_iterations is not None:
        raise ValueError("Caller max_iterations overrides are forbidden in category mode.")
    table = cfg["categories"]
    if not isinstance(table, dict) or set(table) != set(CATEGORY_LIMITS):
        raise ValueError("delegation.categories must define simples, analitica and complexa exactly.")
    routes = {}
    for category, default_limit in CATEGORY_LIMITS.items():
        row = table[category]
        if not isinstance(row, dict) or set(row) - _CONFIG_FIELDS:
            raise ValueError(f"Invalid configuration fields for category {category}.")
        model, provider = row.get("model"), row.get("provider")
        for name, value in (("model", model), ("provider", provider)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"Category {category} requires an exact nonempty {name}.")
        allowed = row.get("allowed_models")
        if (not isinstance(allowed, list) or not allowed
                or any(not isinstance(item, str) or not item or item != item.strip() for item in allowed)
                or model not in allowed):
            raise ValueError(f"Category {category} model must match allowed_models exactly.")
        effort = row.get("reasoning_effort")
        if not isinstance(effort, str) or effort not in ("low", "medium", "high"):
            raise ValueError(f"Category {category} requires valid reasoning_effort.")
        fallback = row.get("fallback_providers")
        if not isinstance(fallback, list) or fallback:
            raise ValueError(f"Category {category} requires fallback_providers: [].")
        limit = row.get("max_iterations", default_limit)
        if type(limit) is not int or limit <= 0 or limit > default_limit:
            raise ValueError(f"Category {category} max_iterations must be 1..{default_limit}.")
        assert isinstance(model, str) and isinstance(provider, str)
        routes[category] = CategoryRoute(category, model, provider, effort, limit)
    selected = []
    for index, task in enumerate(tasks):
        if set(task) - _TASK_FIELDS:
            raise ValueError(f"Task {index} contains forbidden overrides or unknown fields.")
        category = task.get("category", DEFAULT_CATEGORY)
        if not isinstance(category, str) or category not in routes:
            raise ValueError(f"Task {index} has an invalid category.")
        if task.get("context") is not None and not isinstance(task["context"], str):
            raise ValueError(f"Task {index} context must be a string.")
        selected.append(routes[category])
    return selected


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
