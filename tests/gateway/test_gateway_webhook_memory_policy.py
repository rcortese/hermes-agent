"""Gateway agent-construction tests for webhook memory policy."""

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


class _FakeAgent:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tool_progress_callback = None
        self.step_callback = None
        self.stream_delta_callback = None
        self.interim_assistant_callback = None
        self.status_callback = None
        self.reasoning_config = None
        self.service_tier = None
        self.request_overrides = {}
        _FakeAgent.calls.append(kwargs)

    def run_conversation(self, *args, **kwargs):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 0,
            "tools": [],
            "completed": True,
        }


def _make_runner(monkeypatch):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._get_proxy_url = lambda: None
    runner._resolve_session_agent_runtime = lambda **kwargs: (
        "test-model",
        {"api_key": "test-key", "provider": "test-provider"},
    )
    runner._provider_routing = {}
    runner._resolve_session_reasoning_config = lambda **kwargs: None
    runner._load_service_tier = lambda: None
    runner._session_db = None
    runner._fallback_model = None
    runner._agent_cache = None
    runner._agent_cache_lock = None
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_steering_messages = {}
    runner._pending_steering_lock = None
    runner._reasoning_config = None
    runner._service_tier = None
    runner._cleanup_agent_resources = lambda agent: None
    runner._is_session_run_current = lambda *args, **kwargs: True
    runner.adapters = {}

    class _Hooks:
        loaded_hooks = []

        async def emit(self, *args, **kwargs):
            return None

    runner.hooks = _Hooks()

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run._reload_runtime_env_preserving_config_authority", lambda: None)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    return runner


@pytest.mark.asyncio
async def test_webhook_skip_memory_policy_passes_skip_memory_to_agent(monkeypatch):
    _FakeAgent.calls = []
    runner = _make_runner(monkeypatch)
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:sentinel:delivery-1",
        chat_type="webhook",
        user_id="webhook:sentinel",
        user_name="sentinel",
    )

    result = await runner._run_agent(
        message="Alert: MossDown",
        context_prompt="",
        history=[],
        source=source,
        session_id="s1",
        session_key="k1",
        memory_policy="skip",
    )

    assert result["final_response"] == "ok"
    assert _FakeAgent.calls[-1]["platform"] == "webhook"
    assert _FakeAgent.calls[-1]["user_id"] == "webhook:sentinel"
    assert _FakeAgent.calls[-1]["skip_memory"] is True


@pytest.mark.asyncio
async def test_direct_human_gateway_without_policy_preserves_memory_default(monkeypatch):
    _FakeAgent.calls = []
    runner = _make_runner(monkeypatch)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="telegram:chat-1",
        chat_type="dm",
        user_id="human-1",
        user_name="Rodolfo",
    )

    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="s1",
        session_key="k1",
    )

    assert _FakeAgent.calls[-1]["platform"] == "telegram"
    assert _FakeAgent.calls[-1]["user_id"] == "human-1"
    assert _FakeAgent.calls[-1]["skip_memory"] is False
