"""Offline modular port regressions: no provider, credentials or child runtime."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest
from tools import delegate_tool as dt
from tools.delegation_categories import CATEGORY_LIMITS, validate_category_routes, install_category_loop_budget


def config():
    return {"categories": {name: {"model": "model-" + name, "provider": "openai-codex",
        "reasoning_effort": effort, "allowed_models": ["model-" + name], "fallback_providers": []}
        for name, effort in zip(CATEGORY_LIMITS, ("low", "medium", "high"))}}


def test_defaults_exact_table_and_upstream_task_fields():
    routes = validate_category_routes(config(), [{"goal": "work", "images": [], "output_schema": {}, "group": "g"}])
    assert routes[0].category == "analitica"
    assert routes[0].max_iterations == 80
    assert routes[0].credentials_cfg()["fallback_providers"] == []


@pytest.mark.parametrize("field,value", [("model", " model-simples"), ("provider", ""),
    ("allowed_models", ["other"]), ("fallback_providers", ["other"]), ("max_iterations", 41),
    ("max_iterations", True), ("reasoning_effort", "unknown")])
def test_invalid_route(field, value):
    cfg = config(); cfg["categories"]["simples"][field] = value
    with pytest.raises(ValueError): validate_category_routes(cfg, [{"goal": "work"}])


@pytest.mark.parametrize("task", [{"goal": "work", "category": "wrong"}, {"goal": "work", "model": "override"},
                                  {"goal": "work", "max_iterations": 1000}])
def test_batch_fails_before_credentials(monkeypatch, task):
    monkeypatch.setattr(dt, "_load_config", config)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    resolver = Mock(side_effect=AssertionError("credentials must not be reached"))
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolver)
    result = json.loads(dt.delegate_task(tasks=[{**task, "goal": "Implement a bounded source-only regression"}],
                                      parent_agent=SimpleNamespace(_delegate_depth=0)))
    assert "error" in result
    resolver.assert_not_called()


def test_per_task_routes_reach_modular_builder(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", config)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(dt, "_oneshot_spawn_budget", lambda *a: None)
    monkeypatch.setattr(dt, "_announce_batch", lambda *a: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: (None, None, None, None, None))
    from tools import delegation_live_log
    monkeypatch.setattr(delegation_live_log, "create_live_transcripts", lambda *a, **k: (None, [], []))
    resolved, built = [], []
    def resolve(cfg, parent):
        resolved.append(deepcopy(cfg))
        return {"model": cfg["model"], "provider": cfg["provider"], "base_url": None,
                "api_key": None, "api_mode": None}
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolve)
    def build(**kw):
        built.append(kw)
        return SimpleNamespace(run_conversation=lambda *a, **k: {"api_calls": 1}, max_iterations=kw["max_iterations"])
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_batch", lambda batch, background: json.dumps({"ok": True}))
    tasks = [{"goal": "Implement source-only change " + name, "category": name} for name in CATEGORY_LIMITS]
    assert json.loads(dt.delegate_task(tasks=tasks, parent_agent=SimpleNamespace(_delegate_depth=0)))["ok"]
    assert [c["model"] for c in resolved] == ["model-" + n for n in CATEGORY_LIMITS]
    assert [c["max_iterations"] for c in built] == [40, 80, 120]
    assert [c["routing_cfg"]["reasoning_effort"] for c in built] == ["low", "medium", "high"]
    assert all(c["routing_cfg"]["fallback_providers"] == [] for c in built)


def test_budget_reentry_and_unknown_accounting():
    route = validate_category_routes(config(), [{"goal": "work", "category": "simples"}])[0]
    calls = []
    child = SimpleNamespace(max_iterations=40)
    def run(*args, **kwargs):
        calls.append((child.max_iterations, kwargs))
        return {"api_calls": 25, "completed": False}
    child.run_conversation = run
    install_category_loop_budget(child, route)
    child.run_conversation(allow_compression=False)
    child.run_conversation(allow_compression=False)
    assert child.run_conversation()["category_budget_exhausted"]
    assert calls == [(40, {"allow_compression": False}), (15, {"allow_compression": False})]
    assert child._delegation_category_api_calls == 50
    # Reported calls may exceed allowance; this is not a transport cap.
    child = SimpleNamespace(run_conversation=Mock(return_value={}), max_iterations=40)
    original = child.run_conversation
    install_category_loop_budget(child, route)
    child.run_conversation(); child.run_conversation()
    original.assert_called_once()
    assert child._delegation_category_accounting_unknown


def test_upstream_request_id_targets_only_one_pending():
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry
    import threading
    session = "synthetic-category-approval"
    one = _ApprovalEntry({"request_id": "one"})
    two = _ApprovalEntry({"request_id": "two"})
    with approval._lock: approval._gateway_queues[session] = [one, two]
    try:
        assert approval.resolve_gateway_approval(session, "once", request_id="two") == 1
        assert two.event.is_set() and not one.event.is_set()
        assert approval.resolve_gateway_approval(session, "deny", request_id="missing") == 0
    finally:
        with approval._lock: approval._gateway_queues.pop(session, None)
    assert "category" in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
