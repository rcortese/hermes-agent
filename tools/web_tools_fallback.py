"""Explicit ordered search policy. Configured chains never invoke implicit rescue."""
import logging
from copy import deepcopy

logger = logging.getLogger("tools.web_tools")
_BUILTINS = {"firecrawl", "parallel", "tavily", "exa", "searxng", "brave-free", "ddgs", "perplexity", "keenable"}


def _normalize_backend_names(raw):
    from tools.web_tools import _registered_web_provider
    parts = raw.replace(";", ",").split(",") if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    names = []
    for part in parts:
        name = str(part).strip().lower()
        if name and name not in names and (name in _BUILTINS or _registered_web_provider(name) is not None):
            names.append(name)
    return names


def _get_search_fallback_backends(primary_backend=""):
    from tools.web_tools import _load_web_config
    return [name for name in _normalize_backend_names(_load_web_config().get("search_fallback_backends"))
            if name != primary_backend.strip().lower()]


def ordered_search(primary, backend, query, limit):
    from tools.web_tools import _is_backend_available
    from agent.web_search_registry import get_provider
    providers, seen = [], set()
    for candidate in [primary] + [get_provider(n) for n in _get_search_fallback_backends(getattr(primary, "name", backend or ""))
                                 if _is_backend_available(n)]:
        if candidate is not None and candidate.name not in seen and candidate.supports_search():
            seen.add(candidate.name)
            providers.append(candidate)
    failures = []
    for candidate in providers:
        try:
            response = candidate.search(query, limit)
            if not isinstance(response, dict):
                raise ValueError("returned non-dict response")
            if response.get("success") is False:
                raise ValueError(str(response.get("error") or "success=false"))
        except Exception as exc:
            failures.append(f"{candidate.name}: {exc}")
            continue
        response = deepcopy(response)
        if candidate is not primary:
            metadata = response.get("metadata")
            if not isinstance(metadata, dict):
                metadata = response["metadata"] = {}
            metadata.update(backend=candidate.name, fallback_from=getattr(primary, "name", backend), fallback_failures=failures)
        return response
    return {"success": False, "error": "All configured web search providers failed: " + "; ".join(failures)}
