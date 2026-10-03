"""Behavior contracts for fork-local per-task category delegation."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import delegate_tool as delegate
from tools.delegation_categories import (
    CATEGORY_LIMITS, CategoryRoute, install_category_loop_budget,
    validate_category_routes,
)


def config():
    return {"categories": {
        name: {"model": f"model-{name}", "provider": f"provider-{name}",
               "reasoning_effort": effort, "allowed_models": [f"model-{name}"],
               "fallback_providers": []}
        for name, effort in zip(CATEGORY_LIMITS, ["low", "medium", "high"])
    }}


@pytest.fixture
def harness(monkeypatch):
    cfg = config()
    parent = SimpleNamespace(_delegate_depth=0, _interrupt_requested=False,
                             _safe_print=lambda *args: None)
    monkeypatch.setattr(delegate, "_load_config", lambda: cfg)
    credentials = Mock(side_effect=lambda row, parent: {
        "model": row.get("model"), "provider": row.get("provider"),
        "base_url": "https://example.invalid/v1", "api_key": "test-only",
        "api_mode": "chat_completions",
    })
    build = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    run = Mock(side_effect=lambda index, goal, child, parent: {
        "task_index": index, "status": "completed", "summary": goal,
        "api_calls": 1, "model": child.model,
    })
    monkeypatch.setattr(delegate, "_resolve_delegation_credentials", credentials)
    monkeypatch.setattr(delegate, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(delegate, "_run_single_child", run)
    monkeypatch.setattr(delegate, "_finalize_child_results", lambda *args: None)
    from tools import delegation_live_log
    transcripts = Mock(return_value=(None, [], []))
    monkeypatch.setattr(delegation_live_log, "create_live_transcripts", transcripts)
    monkeypatch.setattr(delegation_live_log, "update_manifest_statuses", lambda *args: None)
    delegate.set_spawn_paused(False)
    yield SimpleNamespace(cfg=cfg, parent=parent, credentials=credentials,
                          build=build, run=run, transcripts=transcripts)
    delegate.set_spawn_paused(False)


def test_mixed_batch_and_receipts(harness):
    before = deepcopy(harness.cfg)
    tasks = [{"goal": name, "category": name} for name in CATEGORY_LIMITS]
    original = deepcopy(tasks)
    result = json.loads(delegate.delegate_task(tasks=tasks, parent_agent=harness.parent))
    assert [entry["category"] for entry in result["results"]] == list(CATEGORY_LIMITS)
    for index, (name, limit) in enumerate(CATEGORY_LIMITS.items()):
        kwargs = harness.build.call_args_list[index].kwargs
        assert kwargs["max_iterations"] == limit
        assert kwargs["model"] == f"model-{name}"
        assert kwargs["override_provider"] == f"provider-{name}"
        assert kwargs["toolsets"] is None
        entry = result["results"][index]
        assert entry["configured_max_iterations"] == limit
        assert entry["reasoning_effort"] == harness.cfg["categories"][name]["reasoning_effort"]
        assert entry["configured_provider"] == f"provider-{name}"
    assert [call.args[0] for call in harness.credentials.call_args_list] == [
        {"model": f"model-{name}", "provider": f"provider-{name}",
         "reasoning_effort": harness.cfg["categories"][name]["reasoning_effort"],
         "fallback_providers": []}
        for name in CATEGORY_LIMITS
    ]
    assert harness.cfg == before
    assert tasks == original


@pytest.mark.parametrize("shape", ["single", "batch"])
def test_default_analitica(harness, shape):
    if shape == "single":
        raw = delegate.delegate_task(goal="inspect", parent_agent=harness.parent)
    else:
        raw = delegate.delegate_task(tasks=[{"goal": "inspect"}], parent_agent=harness.parent)
    result = json.loads(raw)
    assert result["results"][0]["category"] == "analitica"
    assert harness.build.call_args.kwargs["max_iterations"] == 80


@pytest.mark.parametrize("category", [None, "", "simple", "ANALITICA", " analitica", 42, []])
def test_invalid_category_atomic(harness, category):
    result = json.loads(delegate.delegate_task(tasks=[
        {"goal": "valid", "category": "simples"},
        {"goal": "bad", "category": category},
    ], parent_agent=harness.parent))
    assert "error" in result
    harness.credentials.assert_not_called()
    harness.transcripts.assert_not_called()
    harness.build.assert_not_called()


@pytest.mark.parametrize("field", ["model", "provider", "max_iterations", "base_url",
                                   "api_key", "reasoning_effort", "toolsets", "credentials_cfg",
                                   "acp_command", "unknown"])
def test_task_overrides_rejected_atomically(harness, field):
    result = json.loads(delegate.delegate_task(tasks=[
        {"goal": "valid"}, {"goal": "bad", field: "override"}
    ], parent_agent=harness.parent))
    assert "error" in result
    harness.credentials.assert_not_called()
    harness.transcripts.assert_not_called()
    harness.build.assert_not_called()


@pytest.mark.parametrize("change", [
    {"model": "MODEL-simples"}, {"model": "model-simples "},
    {"allowed_models": ["model"]}, {"allowed_models": "model-simples"},
    {"provider": ""}, {"reasoning_effort": True}, {"reasoning_effort": False},
    {"reasoning_effort": "invalid"}, {"reasoning_effort": " low"},
    {"reasoning_effort": "high "}, {"reasoning_effort": "LOW"},
    {"reasoning_effort": 1}, {"reasoning_effort": []},
    {"fallback_providers": None}, {"fallback_providers": {}},
    {"fallback_providers": [{"model": "expensive", "provider": "other"}]},
    {"max_iterations": True}, {"max_iterations": 41}, {"max_iterations": 0},
    {"base_url": "https://example.invalid"}, {"api_key": "test"},
])
def test_entire_table_is_validated_even_if_unused(harness, change):
    harness.cfg["categories"]["simples"].update(change)
    result = json.loads(delegate.delegate_task(goal="default", parent_agent=harness.parent))
    assert "error" in result
    harness.credentials.assert_not_called()
    harness.build.assert_not_called()


@pytest.mark.parametrize("table", [None, {}, [], {"simples": {}}])
def test_present_invalid_table_fails_closed(harness, table):
    harness.cfg["categories"] = table
    assert "error" in json.loads(delegate.delegate_task(goal="test", parent_agent=harness.parent))
    harness.credentials.assert_not_called()


def test_limit_override_rejected(harness):
    assert "error" in json.loads(delegate.delegate_task(
        goal="test", max_iterations=80, parent_agent=harness.parent))
    harness.credentials.assert_not_called()


def test_credential_failure_precedes_transcripts_and_spawn(harness):
    harness.credentials.side_effect = [
        {"model": "model-simples"}, ValueError("Credential unavailable")]
    result = json.loads(delegate.delegate_task(tasks=[
        {"goal": "one", "category": "simples"}, {"goal": "two"}
    ], parent_agent=harness.parent))
    assert "Credential unavailable" in result["error"]
    harness.transcripts.assert_not_called()
    harness.build.assert_not_called()


def test_legacy_without_table_preserves_credentials_and_limit(harness):
    harness.cfg.clear()
    harness.cfg.update({"max_iterations": 17, "model": "legacy", "provider": "legacy-provider"})
    result = json.loads(delegate.delegate_task(goal="legacy", max_iterations=99,
                                             parent_agent=harness.parent))
    assert "category" not in result["results"][0]
    assert harness.build.call_args.kwargs["max_iterations"] == 17
    assert "category_route" not in harness.build.call_args.kwargs
    assert harness.credentials.call_args.args[0] is harness.cfg


def test_category_without_table_rejected(harness):
    harness.cfg.clear()
    result = json.loads(delegate.delegate_task(tasks=[{"goal": "test", "category": "simples"}],
                                             parent_agent=harness.parent))
    assert "error" in result
    harness.build.assert_not_called()


def test_existing_pause_depth_and_three_child_controls(harness):
    delegate.set_spawn_paused(True)
    assert "error" in json.loads(delegate.delegate_task(goal="test", parent_agent=harness.parent))
    delegate.set_spawn_paused(False)
    harness.parent._delegate_depth = 1
    assert "error" in json.loads(delegate.delegate_task(goal="test", parent_agent=harness.parent))
    harness.parent._delegate_depth = 0
    assert "error" in json.loads(delegate.delegate_task(
        tasks=[{"goal": "test"}] * 4, parent_agent=harness.parent))
    harness.build.assert_not_called()


def test_shared_retry_allowance_exhaustion_and_flags():
    seen_limits = []
    responses = [
        {"api_calls": 2, "completed": False, "interrupted": False,
         "error": "tool schema rejection", "final_response": "", "messages": []},
        {"api_calls": 1, "completed": False, "interrupted": True,
         "final_response": "partial", "messages": []},
    ]
    child = SimpleNamespace(max_iterations=3)
    def run(**kwargs):
        seen_limits.append(child.max_iterations)
        return responses.pop(0)
    child.run_conversation = run
    route = CategoryRoute("simples", "model", "provider", "low", 3)
    install_category_loop_budget(child, route)
    first = child.run_conversation(user_message="test")
    assert first["completed"] is False and first["interrupted"] is False
    second = child.run_conversation(user_message="schema retry")
    assert second["interrupted"] is True and second["final_response"] == "partial"
    third = child.run_conversation(user_message="no remaining budget")
    assert third["category_budget_exhausted"] is True
    assert third["interrupted"] is True and third["final_response"] == "partial"
    assert seen_limits == [3, 1]
    assert child._delegation_category_api_calls == 3


@pytest.mark.parametrize("response", [{}, {"api_calls": -1}, {"api_calls": True}, {"api_calls": "1"}])
def test_unknown_retry_accounting_fails_closed_without_fabricated_usage(response):
    run = Mock(return_value=response)
    child = SimpleNamespace(run_conversation=run, max_iterations=3)
    install_category_loop_budget(child, CategoryRoute("simples", "model", "provider", "low", 3))
    assert child.run_conversation() is response
    assert child.run_conversation()["category_budget_exhausted"] is True
    assert child._delegation_category_api_calls == 0
    assert child._delegation_category_accounting_unknown is True
    assert run.call_count == 1


def test_exception_prevents_unaccounted_retry():
    run = Mock(side_effect=RuntimeError("partial attempt"))
    child = SimpleNamespace(run_conversation=run, max_iterations=3)
    install_category_loop_budget(child, CategoryRoute("simples", "model", "provider", "low", 3))
    with pytest.raises(RuntimeError, match="partial attempt"):
        child.run_conversation()
    with pytest.raises(RuntimeError, match="partial attempt"):
        child.run_conversation()
    assert run.call_count == 1
    assert child._delegation_category_api_calls == 0


def test_real_child_builder_reasoning_and_no_fallback(monkeypatch):
    """Real builder path, substituting only the external agent constructor."""
    import run_agent
    captured = {}
    def constructor(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_conversation=lambda **kw: {"api_calls": 1},
                               session_id="test-child")
    monkeypatch.setattr(run_agent, "AIAgent", constructor)
    monkeypatch.setattr(delegate, "_load_config", lambda: {"reasoning_effort": "high"})
    monkeypatch.setattr(delegate, "_resolve_child_credential_pool", lambda *args: None)
    parent = SimpleNamespace(model="parent", provider="parent-provider", base_url="https://parent.invalid",
                             api_key="test-parent", enabled_toolsets=["file"], reasoning_config={"effort": "high"},
                             _fallback_chain=[{"model": "expensive"}], _delegate_depth=0)
    routes = validate_category_routes(config(), [{"goal": "test", "category": "simples"}])
    assert routes is not None
    route = routes[0]
    delegate._build_child_agent(0, "test", None, None, route.model, route.max_iterations, 1,
                               parent, override_provider=route.provider,
                               override_base_url="https://child.invalid", override_api_key="test-child",
                               category_route=route)
    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["fallback_model"] == []
    assert captured["max_iterations"] == 40
    assert captured["skip_memory"] is True and captured["platform"] == "subagent"
    assert captured["acp_command"] is None
    assert parent.reasoning_config == {"effort": "high"}
    assert parent._fallback_chain == [{"model": "expensive"}]


def test_model_hidden_fields_are_rejected_not_scrubbed(harness):
    tasks = [{"goal": "test", "acp_command": "untrusted"}]
    assert delegate._strip_model_hidden_task_fields(tasks) is tasks
    assert "error" in json.loads(delegate.delegate_task(
        tasks=delegate._strip_model_hidden_task_fields(tasks), parent_agent=harness.parent))
    harness.build.assert_not_called()


@pytest.mark.parametrize("category,effort,limit", [
    ("simples", "low", 40), ("analitica", "medium", 80), ("complexa", "high", 120),
])
@pytest.mark.parametrize("outcome", ["completed", "error", "interrupted"])
def test_real_config_resolver_and_agent_path(tmp_path, monkeypatch, category, effort, limit, outcome):
    """Use real temp config, credential resolver, builder and worker lifecycle.

    Replace provider resolution and inference only; never contact a live API.
    """
    import yaml
    import run_agent
    from hermes_cli import runtime_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = config()
    for row in cfg["categories"].values():
        row["provider"] = "openrouter"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(
        {"delegation": cfg, "model": {"context_length": 128000}}
    ))
    resolver = Mock(return_value={
        "provider": "openrouter", "base_url": "https://example.invalid/v1",
        "api_key": "test-only-not-a-live-key", "api_mode": "chat_completions",
    })
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolver)
    monkeypatch.setattr(delegate, "_resolve_child_credential_pool", lambda *args: None)
    observed = []
    def inference(self, **kwargs):
        observed.append((self.model, self.provider, self.reasoning_config,
                         self.max_iterations, self._fallback_chain, self._memory_manager))
        return {"final_response": "local transport test", "api_calls": 1,
                "completed": outcome == "completed", "interrupted": outcome == "interrupted",
                "error": "original transport error" if outcome == "error" else None,
                "messages": []}
    monkeypatch.setattr(run_agent.AIAgent, "run_conversation", inference)
    parent = SimpleNamespace(
        model="parent", provider="openrouter", base_url="https://parent.invalid",
        api_key="test-parent", enabled_toolsets=["file"], _delegate_depth=0,
        _interrupt_requested=False, reasoning_config={"effort": "high"},
        _fallback_chain=[{"model": "expensive", "provider": "anthropic"}],
    )
    result = json.loads(delegate.delegate_task(
        tasks=[{"goal": "inspect", "category": category}], parent_agent=parent))
    entry = result["results"][0]
    assert entry["status"] == ("failed" if outcome == "error" else outcome), result
    if outcome == "error":
        assert entry["error"] == "original transport error"
        assert entry["exit_reason"] == "error"
    assert entry["configured_model"] == f"model-{category}"
    assert entry["configured_provider"] == "openrouter"
    assert entry["provider"] == "openrouter"
    assert entry["reasoning_effort"] == effort
    assert entry["configured_max_iterations"] == limit
    assert result["results"][0]["api_calls"] == 1
    assert observed == [(f"model-{category}", "openrouter", {"enabled": True, "effort": effort}, limit, [], None)]
    assert resolver.call_args.kwargs == {"requested": "openrouter", "target_model": f"model-{category}"}


def test_exhaustion_preserves_original_error_without_retry():
    response = {"api_calls": 3, "completed": False, "interrupted": False,
                "error": "schema rejected by transport", "final_response": "partial"}
    run = Mock(return_value=response)
    child = SimpleNamespace(run_conversation=run, max_iterations=3)
    install_category_loop_budget(child, CategoryRoute("simples", "model", "provider", "low", 3))
    assert child.run_conversation() is response
    exhausted = child.run_conversation(user_message="external re-entry")
    assert exhausted == dict(response, category_budget_exhausted=True)
    run.assert_called_once_with()
    assert child._delegation_category_api_calls == 3


def test_receipt_does_not_replace_effective_identity(harness):
    harness.run.side_effect = lambda index, goal, child, parent: {
        "task_index": index, "status": "completed", "summary": goal,
        "api_calls": 1, "model": "effective-model", "provider": "effective-provider",
    }
    entry = json.loads(delegate.delegate_task(goal="inspect", parent_agent=harness.parent))["results"][0]
    assert entry["model"] == "effective-model" and entry["provider"] == "effective-provider"
    assert entry["configured_model"] == "model-analitica"
    assert entry["configured_provider"] == "provider-analitica"


@pytest.mark.parametrize("field", ["output_schema", "images", "group"])
def test_unimplemented_runtime_task_fields_rejected(harness, field):
    result = json.loads(delegate.delegate_task(tasks=[{"goal": "inspect", field: {}}],
                                             parent_agent=harness.parent))
    assert "error" in result
    harness.credentials.assert_not_called()
    harness.build.assert_not_called()


def test_missing_fallback_field_is_invalid(harness):
    del harness.cfg["categories"]["complexa"]["fallback_providers"]
    assert "error" in json.loads(delegate.delegate_task(goal="inspect", parent_agent=harness.parent))
    harness.credentials.assert_not_called()


def test_documented_config_is_accepted_exactly():
    from pathlib import Path
    import yaml
    doc = (Path(__file__).parents[2] / "website/docs/user-guide/features/delegation.md").read_text()
    example = doc.split("## Fork-local task categories", 1)[1].split("```yaml\n", 1)[1].split("```", 1)[0]
    cfg = yaml.safe_load(example)["delegation"]
    routes = validate_category_routes(cfg, [{"goal": name, "category": name} for name in CATEGORY_LIMITS])
    assert routes is not None
    assert [route.model for route in routes] == ["gpt-6-luna", "gpt-6-luna", "gpt-6.1-sol"]
    assert [route.reasoning_effort for route in routes] == ["low", "medium", "high"]
    assert [route.max_iterations for route in routes] == [40, 80, 120]
