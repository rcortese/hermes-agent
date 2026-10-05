"""Owner-scoped development delegation: offline validation and native failover rails."""
from copy import deepcopy
import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
import yaml

from tools import delegate_tool as dt
from tools.delegation_categories import (
    CATEGORY_LIMITS, category_execution_receipt, install_category_loop_budget,
    validate_category_routes,
)
from tools.delegate_tool_child_run import _ChildRun, _SchemaOutcome, _fabricated_entry
from tools.delegate_tool_config import _resolve_child_fallback_chain
from tests.tools.test_delegate import _make_mock_parent

KIMI = {"provider": "custom:kimi-api", "model": "k3-256k"}
CODEX = {"provider": "openai-codex", "model": "owner-codex-model"}
KIMI_URL = "https://api.kimi.ai/coding/v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Provider/network calls are forbidden in these tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def owner_config():
    table = {}
    for name, effort in zip(CATEGORY_LIMITS, ("low", "medium", "high")):
        primary, alternative = (KIMI, CODEX) if name == "simples" else (CODEX, KIMI)
        table[name] = {
            "provider": "openrouter", "model": "general-" + name,
            "allowed_models": ["general-" + name], "reasoning_effort": effort,
            "fallback_providers": [],
            "development": {**primary, "allowed_models": [primary["model"]],
                "reasoning_effort": effort, "max_iterations": CATEGORY_LIMITS[name],
                "allowed_routes": [deepcopy(primary), deepcopy(alternative)],
                "fallback_providers": [deepcopy(alternative)]},
        }
    return {"categories": table, "fallback_providers": [{"provider": "nous", "model": "parent-only"}]}


def test_purpose_selects_only_complete_owner_routes_without_inheritance():
    cfg = owner_config()
    original = deepcopy(cfg)
    tasks = [{"goal": "Implement bounded change", "category": category} for category in CATEGORY_LIMITS]
    general = validate_category_routes(cfg, tasks)
    development = validate_category_routes(cfg, [{**t, "purpose": "development"} for t in tasks])
    assert [r.model for r in general] == ["general-" + name for name in CATEGORY_LIMITS]
    assert all(r.purpose == "general" and r.credentials_cfg()["fallback_providers"] == [] for r in general)
    assert [r.credentials_cfg()["provider"] for r in development] == [KIMI["provider"], CODEX["provider"], CODEX["provider"]]
    assert [r.max_iterations for r in development] == list(CATEGORY_LIMITS.values())
    assert development[0].credentials_cfg()["fallback_providers"] == [CODEX]
    assert development[1].credentials_cfg()["fallback_providers"] == [KIMI]
    assert cfg == original
    # Returned dictionaries cannot mutate the owner's immutable validated route.
    development[0].credentials_cfg()["fallback_providers"][0]["model"] = "injected"
    assert development[0].credentials_cfg()["fallback_providers"] == [CODEX]
    parent = SimpleNamespace(_fallback_chain=cfg["fallback_providers"])
    assert _resolve_child_fallback_chain(parent, general[0].credentials_cfg(), pinned=False) is None
    assert _resolve_child_fallback_chain(parent, development[0].credentials_cfg(), pinned=True) == [CODEX]
    # Existing owner reductions remain bounded; callers cannot raise the fixed ceilings.
    cfg["categories"]["simples"]["development"]["max_iterations"] = 10
    assert validate_category_routes(cfg, [{"goal": "work", "category": "simples", "purpose": "development"}])[0].max_iterations == 10
    assert validate_category_routes({}, [{"goal": "work", "purpose": "general"}]) is None
    with pytest.raises(ValueError, match="require"):
        validate_category_routes({}, [{"goal": "work", "purpose": "development"}])


@pytest.mark.parametrize("field", ["model", "provider", "reasoning_effort", "allowed_models", "fallback_providers"])
def test_development_never_inherits_a_missing_required_field(field):
    cfg = owner_config()
    del cfg["categories"]["simples"]["development"][field]
    # Validate unused development rows too, before any general task starts.
    with pytest.raises(ValueError):
        validate_category_routes(cfg, [{"goal": "work"}])


@pytest.mark.parametrize("mutation", [
    {"allowed_routes": None}, {"allowed_routes": []},
    {"allowed_routes": [KIMI]}, {"allowed_routes": [CODEX]},
    {"allowed_routes": [KIMI, {**CODEX, "provider": "wrong-owner"}]},
    {"allowed_routes": [KIMI, {**CODEX, "model": CODEX["model"] + " "}]},
    {"allowed_routes": [KIMI, CODEX, CODEX]},
    {"allowed_routes": [KIMI, {**CODEX, "api_key": "fixture-only"}]},
    {"fallback_providers": None}, {"fallback_providers": ["openai-codex"]},
    {"fallback_providers": [{"provider": "openai-codex"}]},
    {"fallback_providers": [KIMI]}, {"fallback_providers": [CODEX, CODEX]},
    {"fallback_providers": [{**CODEX, "provider": "openai-codex "}]},
    {"fallback_providers": [{**CODEX, "model": "not-approved"}]},
    {"fallback_providers": [{**CODEX, "provider": "not-approved"}]},
    {"allowed_models": ["not-primary"]}, {"max_iterations": True}, {"max_iterations": 41},
    {"development": {}}, {"base_url": KIMI_URL}, {"api_key": "fixture-only"},
])
def test_owner_route_validation_fails_closed(mutation):
    cfg = owner_config()
    cfg["categories"]["simples"]["development"].update(deepcopy(mutation))
    with pytest.raises(ValueError):
        validate_category_routes(cfg, [{"goal": "work", "purpose": "development"}])


@pytest.mark.parametrize("field", ["api_key", "key_env", "api_key_env", "base_url", "api_mode", "token", "request_overrides"])
def test_fallback_pairs_reject_every_transport_or_credential_override(field):
    cfg = owner_config()
    cfg["categories"]["simples"]["development"]["fallback_providers"] = [{**CODEX, field: "fixture-only"}]
    with pytest.raises(ValueError, match="without extra fields"):
        validate_category_routes(cfg, [{"goal": "work"}])


def test_explicit_multi_route_chain_and_legacy_allowed_models():
    cfg = owner_config()
    row = cfg["categories"]["simples"]["development"]
    third = {"provider": "owner-third", "model": "third-model"}
    row["fallback_providers"].append(third)
    row["allowed_routes"].append(third)
    route = validate_category_routes(cfg, [{"goal": "work", "category": "simples", "purpose": "development"}])[0]
    assert route.credentials_cfg()["fallback_providers"] == [CODEX, third]
    assert route.receipt()["configured_chain"] == [KIMI, CODEX, third]
    # Empty fallback keeps the primary-only legacy allowed_models contract.
    row["fallback_providers"] = []
    del row["allowed_routes"]
    assert validate_category_routes(cfg, [{"goal": "work", "purpose": "development"}])
    # New allowed_routes applies equally to an explicitly declared general chain.
    general = cfg["categories"]["simples"]
    general["fallback_providers"] = [CODEX]
    with pytest.raises(ValueError, match="allowed_routes"):
        validate_category_routes(cfg, [{"goal": "work"}])
    general["allowed_routes"] = [{"provider": general["provider"], "model": general["model"]}, CODEX]
    assert validate_category_routes(cfg, [{"goal": "work", "category": "simples"}])[0].credentials_cfg()["fallback_providers"] == [CODEX]


@pytest.mark.parametrize("task,top_images", [
    ({"purpose": "other"}, None), ({"purpose": None}, None), ({"purpose": []}, None),
    ({"purpose": "development", "category": "simples", "model": "inject"}, None),
    ({"purpose": "development", "provider": "inject"}, None),
    ({"purpose": "development", "fallback_providers": [CODEX]}, None),
    ({"purpose": "development", "allowed_routes": [CODEX]}, None),
    ({"purpose": "development", "base_url": KIMI_URL}, None),
    ({"purpose": "development", "api_key": "fixture-only"}, None),
    ({"purpose": "development", "images": ["https://image.invalid/a.png"]}, None),
    ({"purpose": "development"}, ["https://image.invalid/a.png"]),
    ({"purpose": "development", "images": None}, ["https://image.invalid/a.png"]),
])
def test_bad_batch_rejected_before_credentials_or_dispatch(monkeypatch, task, top_images):
    monkeypatch.setattr(dt, "_load_config", owner_config)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    resolver = Mock(side_effect=AssertionError("credential resolution must not run"))
    dispatch = Mock(side_effect=AssertionError("dispatch must not run"))
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolver)
    monkeypatch.setattr(dt, "_run_batch", dispatch)
    result = json.loads(dt.delegate_task(tasks=[{"goal": "Implement bounded source change", **task}],
                        images=top_images, parent_agent=SimpleNamespace(_delegate_depth=0)))
    assert "error" in result
    resolver.assert_not_called()
    dispatch.assert_not_called()


def test_missing_selected_development_row_is_not_general_fallback(monkeypatch):
    cfg = owner_config()
    del cfg["categories"]["analitica"]["development"]
    monkeypatch.setattr(dt, "_load_config", lambda: cfg)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    resolver = Mock(side_effect=AssertionError("credentials must not run"))
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolver)
    result = json.loads(dt.delegate_task(tasks=[{"goal": "Implement bounded source change", "purpose": "development"}],
                        parent_agent=SimpleNamespace(_delegate_depth=0)))
    assert "no configured analitica/development" in result["error"]
    resolver.assert_not_called()


def test_registry_dispatch_real_config_loader_and_child_runtime(tmp_path, monkeypatch):
    """Real registry -> profile loader -> per-task native runtime construction."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import delegation_live_log
    from tools.registry import registry
    cfg = owner_config()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"delegation": cfg}))
    token = set_hermes_home_override(tmp_path)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(dt, "_oneshot_spawn_budget", lambda *args: None)
    monkeypatch.setattr(dt, "_announce_batch", lambda *args: None)
    monkeypatch.setattr(dt, "_capture_origin", lambda: (None, None, None, None, None))
    monkeypatch.setattr(delegation_live_log, "create_live_transcripts", lambda *args, **kwargs: (None, [], []))
    monkeypatch.setattr(dt, "_resolve_child_credential_pool", lambda *args, **kwargs: None)
    resolved, built, batches = [], [], []

    def resolve(row, parent):
        resolved.append(deepcopy(row))
        return {**row, "base_url": KIMI_URL if row["provider"] == KIMI["provider"] else "https://codex.invalid/v1",
                "api_key": "fixture-only", "api_mode": "chat_completions"}

    def construct(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(**{key: kwargs[key] for key in ("model", "provider", "requested_provider", "max_iterations")},
            run_conversation=lambda **kw: {"api_calls": 1, "completed": True},
            _session_init_model_config=None)

    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolve)
    monkeypatch.setattr(dt, "_run_batch", lambda batch, background: batches.append(batch) or json.dumps({"ok": True}))
    tasks = [{"goal": "Implement bounded change for " + name, "category": name, "purpose": "development"}
             for name in CATEGORY_LIMITS]
    tasks.append({"goal": "Analyze general task without changing development routes", "category": "simples"})
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = [{"provider": "nous", "model": "parent-only"}]
    try:
        with patch("run_agent.AIAgent", side_effect=construct):
            entry = registry.get_entry("delegate_task")
            assert entry is not None
            assert json.loads(entry.handler({"tasks": tasks}, parent_agent=parent))["ok"]
    finally:
        reset_hermes_home_override(token)
    assert [row["provider"] for row in resolved] == [KIMI["provider"], CODEX["provider"], CODEX["provider"], "openrouter"]
    assert [row["fallback_model"] for row in built] == [[CODEX], [KIMI], [KIMI], None]
    assert [row["max_iterations"] for row in built] == [40, 80, 120, 40]
    assert built[0]["base_url"] == KIMI_URL
    schema = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert schema["purpose"]["enum"] == ["general", "development"]
    assert schema["purpose"]["default"] == "general"
    assert not {"model", "provider", "fallback_providers", "allowed_routes"} & schema.keys()


@pytest.mark.parametrize("category,reason_name", [("simples", "billing"), ("analitica", "rate_limit")])
@pytest.mark.parametrize("available", [True, False])
def test_native_engine_switches_both_directions_and_exhausts_offline(monkeypatch, category, reason_name, available):
    """Native AIAgent activation and receipt wrapper; only the provider boundary is fake."""
    from agent.error_classifier import FailoverReason
    from run_agent import AIAgent
    from agent import chat_completion_helpers as helpers
    route = validate_category_routes(owner_config(), [{"goal": "work", "category": category, "purpose": "development"}])[0]
    chain = _resolve_child_fallback_chain(SimpleNamespace(_fallback_chain=[{"provider": "nous", "model": "parent"}]),
                                          route.credentials_cfg(), pinned=True)
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        child = AIAgent(api_key="fixture-only", base_url="https://primary.invalid/v1",
                        model=route.model, provider="custom", requested_provider=route.provider,
                        quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=chain,
                        max_iterations=route.max_iterations)
    child.run_conversation = Mock(return_value={"api_calls": 1, "completed": True, "final_response": "done"})
    install_category_loop_budget(child, route)
    fallback_client = MagicMock()
    target = chain[0]
    fallback_client.base_url = KIMI_URL if target["provider"] == KIMI["provider"] else "https://codex.invalid/v1"
    fallback_client.api_key = "fixture-only"
    monkeypatch.setattr(helpers, "_fallback_entry_unavailable_without_network", lambda *args: None)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *args, **kwargs: None)
    resolver = Mock(return_value=(fallback_client if available else None, target["model"]))
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", resolver)
    monkeypatch.setattr("hermes_cli.model_normalize.normalize_model_for_provider", lambda model, provider: model)
    reason = FailoverReason[reason_name]
    try:
        assert child._try_activate_fallback(reason=reason) is available
        expected = target if available else {"provider": route.provider, "model": route.model}
        assert (child.requested_provider, child.model) == (expected["provider"], expected["model"])
        assert child.max_iterations == route.max_iterations
        assert not child._try_activate_fallback(reason=reason)
        resolver.assert_called_once()
        receipt = category_execution_receipt(child)
        assert receipt["configured_chain"] == [{"provider": route.provider, "model": route.model}, target]
        assert receipt["effective_provider"] == expected["provider"]
        transitions = ([{"from": {"provider": route.provider, "model": route.model},
                         "to": target, "reason": reason.value}] if available else [])
        assert receipt["fallback_transitions"] == transitions
    finally:
        child.close()


def test_observation_preserves_signature_enum_and_budget_schema_retry(monkeypatch):
    from agent.error_classifier import FailoverReason
    route = validate_category_routes(owner_config(), [{"goal": "work", "category": "simples", "purpose": "development"}])[0]
    child = SimpleNamespace(provider="custom", requested_provider=KIMI["provider"], model=KIMI["model"], max_iterations=40,
                            _delegate_output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}})
    arguments, calls = [], []

    def switch(reason=None, reset_at=None):
        arguments.append((reason, reset_at))
        child.provider = child.requested_provider = CODEX["provider"]
        child.model = CODEX["model"]
        return True

    def run(**kwargs):
        calls.append((child.max_iterations, kwargs))
        if len(calls) == 1:
            assert child._try_activate_fallback(FailoverReason.rate_limit, "reset-marker")
            return {"api_calls": 39, "final_response": "not json", "messages": [], "completed": False}
        return {"api_calls": 1, "final_response": '{"ok": true}', "messages": [], "completed": True}

    child._try_activate_fallback = switch
    child.run_conversation = run
    install_category_loop_budget(child, route)
    result = child.run_conversation(allow_compression=False)
    schema = dt._validate_child_output_schema(child, result, 0, "offline-child", None)
    assert schema.valid and schema.retries == 1
    assert [limit for limit, _ in calls] == [40, 1]
    assert calls[0][1] == {"allow_compression": False}
    assert arguments == [(FailoverReason.rate_limit, "reset-marker")]
    assert child.run_conversation()["category_budget_exhausted"]
    assert len(calls) == 2
    entry = dt._build_result_entry(child, result, 0, 0.1, schema)
    assert entry["api_calls"] == 40
    assert entry["purpose"] == "development" and entry["effective_provider"] == CODEX["provider"]
    assert entry["fallback_transitions"][0]["reason"] == "rate_limit"
    assert child._try_activate_fallback(reason="secret-bearing raw diagnostic")
    receipt = category_execution_receipt(child)
    assert receipt["fallback_transitions"][-1]["reason"] == "unknown"
    assert "secret-bearing" not in json.dumps(receipt)
    # Completion/error receipts are detached snapshots, not live mutable lists.
    assert len(entry["fallback_transitions"]) == 1
    failure = _fabricated_entry(0, "error", "offline failure", child)
    assert failure["purpose"] == "development"
    timeout = {"task_index": 0, "status": "timeout", "summary": None, "error": "offline timeout",
               "duration_seconds": 0, "api_calls": 17}
    runner = _ChildRun(child, None, 0, "work", None, None)
    assert runner.finish_failed(timeout, None, preview="timeout")["api_calls"] == 17
    assert timeout["effective_model"] == CODEX["model"]
    legacy = SimpleNamespace(model="legacy-model")
    assert category_execution_receipt(legacy) == {}
    legacy_entry = dt._build_result_entry(legacy, {"completed": True, "final_response": "done"}, 0, 0,
                                         _SchemaOutcome(None, None, [], 0))
    assert "purpose" not in legacy_entry
