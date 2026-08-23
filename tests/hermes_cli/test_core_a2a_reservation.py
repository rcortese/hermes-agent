"""Causal guards for the restricted, core-owned A2A/9900 surface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PUBLIC_A2A_TOOLS = (
    "a2a_call",
    "a2a_discover",
    "a2a_list",
    "a2a_history",
    "a2a_orchestrate",
)
FORBIDDEN_A2A_TOOLS = ("a2a_fanout",)


def _manifest(**overrides) -> PluginManifest:
    values = {
        "name": "ordinary-plugin",
        "key": "ordinary-plugin",
        "source": "user",
        "kind": "standalone",
    }
    values.update(overrides)
    return PluginManifest(**values)  # type: ignore[arg-type]


def _context(tmp_path) -> PluginContext:
    return PluginContext(_manifest(), PluginManager())


@pytest.mark.parametrize("name", PUBLIC_A2A_TOOLS)
def test_plugin_context_rejects_each_reserved_core_a2a_name(tmp_path, name):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    with pytest.raises(ReservedCoreA2ANameError, match=name):
        _context(tmp_path).register_tool(
            name=name,
            toolset="ordinary",
            schema={},
            handler=lambda _: "unexpected",
            override=True,
        )


@pytest.mark.parametrize("name", FORBIDDEN_A2A_TOOLS)
def test_plugin_context_does_not_reserve_unapproved_a2a_apis(tmp_path, name):
    """Only the five reviewed APIs are core-owned; fan-out remains unavailable."""
    _context(tmp_path).register_tool(
        name=name,
        toolset="ordinary",
        schema={},
        handler=lambda _: "ordinary plugin tool",
    )


def test_discovery_rejects_the_only_reserved_a2a_tool_before_plugin_execution(monkeypatch):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    manager = PluginManager()
    executed = []
    manifest = _manifest(provides_tools=["a2a_call"])
    monkeypatch.setattr(manager, "_scan_directory", lambda *args, **kwargs: [manifest])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(manager, "_load_plugin", lambda candidate: executed.append(candidate))

    with pytest.raises(ReservedCoreA2ANameError, match="a2a_call"):
        manager._discover_and_load_inner()
    assert executed == []


def test_core_registration_exposes_the_five_restricted_a2a_tools():
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import register_core_a2a_builtin
    from tools.registry import registry

    for name in (*PUBLIC_A2A_TOOLS, *FORBIDDEN_A2A_TOOLS):
        registry._tools.pop(name, None)
    platform_registry.unregister("a2a")
    register_core_a2a_builtin()

    platform = platform_registry.get("a2a")
    assert platform is not None and platform.source == "builtin"
    assert {name for name in (*PUBLIC_A2A_TOOLS, *FORBIDDEN_A2A_TOOLS) if registry.get_entry(name)} == set(PUBLIC_A2A_TOOLS)


def test_ordinary_plugin_cannot_replace_core_a2a_platform_slot():
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import ReservedCoreA2ANameError, register_core_a2a_builtin

    platform_registry.unregister("a2a")
    register_core_a2a_builtin()
    core_entry = platform_registry.get("a2a")
    assert core_entry is not None and core_entry.source == "builtin"

    with pytest.raises(ReservedCoreA2ANameError, match="a2a"):
        _context(None).register_platform(
            name="a2a",
            label="Imposter A2A",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
        )

    assert platform_registry.get("a2a") is core_entry


def test_plugin_context_registers_non_core_platform():
    from gateway.platform_registry import platform_registry

    name = "ordinary-platform"
    platform_registry.unregister(name)
    try:
        _context(None).register_platform(
            name=name,
            label="Ordinary Platform",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
        )
        entry = platform_registry.get(name)
        assert entry is not None
        assert entry.source == "plugin"
        assert entry.plugin_name == "ordinary-plugin"
    finally:
        platform_registry.unregister(name)


def test_restricted_client_schema_has_no_peer_or_url_control():
    from plugins.platforms.a2a import tools

    assert set(tools._SCHEMAS) == set(PUBLIC_A2A_TOOLS)
    schema = tools._SCHEMAS["a2a_call"].get("function")
    assert schema is not None
    params = schema.get("parameters")
    assert params is not None
    assert set(params["properties"]) == {"message", "context_id"}
    assert params["additionalProperties"] is False
    for name, wrapped in tools._SCHEMAS.items():
        properties = wrapped["function"]["parameters"]["properties"]
        assert not set(properties).intersection({"agent", "peer", "url", "endpoint", "path", "credential", "token", "fanout"})
    assert tools._DENHOLM_ALIAS == "denholm"
    assert tools._DENHOLM_ENDPOINT == "http://denholm:9900"


def test_restricted_discovery_and_list_only_describe_fixed_denholm():
    from plugins.platforms.a2a import tools

    discovery = json.loads(tools.a2a_discover({}))
    listing = json.loads(tools.a2a_list({}))

    assert discovery == {
        "peer": "denholm",
        "endpoint": "http://denholm:9900",
        "discovery": "disabled",
    }
    assert listing["peers"] == [discovery]


def test_restricted_history_reads_only_local_persisted_context(monkeypatch):
    from plugins.platforms.a2a import tools

    monkeypatch.setattr(
        tools.protocol,
        "load_conversation",
        lambda context_id, limit: [{"role": "user", "text": context_id}],
    )

    result = json.loads(tools.a2a_history({"context_id": "ctx-1"}))

    assert result == {
        "peer": "denholm",
        "context_id": "ctx-1",
        "messages": [{"role": "user", "text": "ctx-1"}],
    }


def test_restricted_orchestration_delegates_to_one_fixed_denholm_call(monkeypatch):
    from plugins.platforms.a2a import tools

    seen = {}
    monkeypatch.setattr(tools, "a2a_call", lambda args, **kwargs: seen.setdefault("args", args) or "unexpected")

    tools.a2a_orchestrate({"message": "one task", "context_id": "ctx-1"})

    assert seen["args"] == {"message": "one task", "context_id": "ctx-1"}


def test_restricted_client_rejects_caller_selected_endpoint():
    from plugins.platforms.a2a import tools

    assert "caller-selected endpoint" in tools.a2a_call({"message": "hi", "url": "http://elsewhere"})
    assert "caller-selected endpoint" in tools.a2a_call({"message": "hi", "agent": "http://elsewhere"})
    assert "caller-selected endpoint" in tools.a2a_call({"message": "hi", "credential": "caller-secret"})
    assert "not permitted" in tools.a2a_history({"context_id": "ctx-1", "path": "/tmp/history"})
    assert "not permitted" in tools.a2a_orchestrate({"message": "hi", "peers": ["other"]})


def test_restricted_client_posts_only_to_fixed_endpoint_with_configured_bearer(monkeypatch):
    from plugins.platforms.a2a import tools

    seen = {}

    class Response:
        def read(self):
            return json.dumps({"result": {"parts": [{"text": "ack"}]}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def open_fixed(request, *, timeout):
        seen.update(url=request.full_url, auth=request.get_header("Authorization"), timeout=timeout)
        return Response()

    monkeypatch.setattr(
        tools,
        "_load_config",
        lambda: {"a2a_agents": {"denholm": {"auth": {"type": "bearer", "token": "config-ref"}}}},
    )
    monkeypatch.setattr(tools._NO_REDIRECT_OPENER, "open", open_fixed)

    assert "ack" in tools.a2a_call({"message": "hi"})
    assert seen["url"] == "http://denholm:9900"
    assert seen["auth"] == "Bearer config-ref"


def test_redirect_handler_rejects_redirects_instead_of_retargeting_call():
    from plugins.platforms.a2a import tools

    request = urllib.request.Request("http://denholm:9900", method="POST")
    with pytest.raises(urllib.error.HTTPError, match="redirects are not permitted"):
        tools._RejectRedirects().redirect_request(
            request, cast(Any, None), 302, "Found", cast(Any, {}), "http://elsewhere"
        )


def test_a2a_package_retains_default_port_9900():
    from plugins.platforms.a2a.adapter import _DEFAULT_PORT

    assert _DEFAULT_PORT == 9900
